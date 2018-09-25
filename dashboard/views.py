import json
import csv
import io
import os
import codecs
import datetime
import shutil
import logging
from functools import wraps
from xml.sax.saxutils import escape

from urlparse import urlparse, urljoin
from flask import session as flask_session
from flask import render_template, flash, url_for, redirect, request, jsonify, \
        abort, g, make_response, send_file
from flask_login import login_user, logout_user, current_user, \
        login_required, fresh_login_required
from flask_mail import Message
from sqlalchemy.exc import SQLAlchemyError
from oauth import OAuthSignIn
from github import Github, GithubException

import datman as dm
from dashboard import app, db, lm
from . import utils
from . import redcap as REDCAP
from .queries import query_metric_values_byid, query_metric_types, \
        query_metric_values_byname
from .models import Study, Site, Session, Scantype, Scan, User, \
        Timepoint, AnalysisComment, Analysis, IncidentalFinding, StudyUser, \
        SessionRedcap, EmptySession
from .forms import SelectMetricsForm, StudyOverviewForm, \
        ScanBlacklistForm, UserForm, ScanCommentForm, AnalysisForm, \
        UserAdminForm, EmptySessionForm, IncidentalFindingsForm, \
        TimepointCommentsForm
from .view_utils import get_user_form, report_form_errors, get_timepoint, \
        get_session
from .emails import incidental_finding_email

logger = logging.getLogger(__name__)
logger.info('Loading views')


class InvalidUsage(Exception):
    """
    Generic exception for API
    """
    status_code = 400

    def __init__(self, message, status_code=None, payload=None):
        Exception.__init__(self)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        self.payload = payload

    def to_dict(self):
        rv = dict(self.payload or ())
        rv['message'] = self.message
        return rv

@app.errorhandler(InvalidUsage)
def handle_invalid_usage(error):
    response = jsonify(error.to_dict())
    response.status_code = error.status_code
    return response

@lm.user_loader
def load_user(id):
    return User.query.get(int(id))

def login_required(f):
    """
    Checks the requester has a valid authentication cookie
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

def study_admin_required(f):
    """
    Verifies a user is a study admin or a dashboard admin. Any view function
    this wraps must have 'study_id' as an argument
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_study_admin(kwargs['study_id']):
            flash("Not authorized.")
            return redirect(prev_url())
        return f(*args, **kwargs)
    return decorated_function

def prev_url():
    """
    Returns the referring page if it is safe to do so, otherwise directs
    the user to the index.
    """
    if request.referrer and is_safe_url(request.referrer):
        return request.referrer
    return url_for('index')

def is_safe_url(target):
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ('http', 'https') and \
           ref_url.netloc == test_url.netloc

@app.before_request
def before_request():
    if current_user.is_authenticated:
        if not current_user.is_active:
            logout_user()
            flash('Your account is disabled. Please contact an administrator.')
            return
        db.session.add(current_user)
        db.session.commit()

@app.route('/logout')
def logout():
    logout_user()
    flash('You have been logged out.')
    return redirect(url_for('login'))

@app.route('/')
@app.route('/index')
@login_required
def index():
    """
    Main landing page
    """
    studies = current_user.get_studies()

    timepoint_count = Timepoint.query.count()
    study_count = Study.query.count()
    site_count = Site.query.count()
    return render_template('index.html',
                           studies=studies,
                           timepoint_count=timepoint_count,
                           study_count=study_count,
                           site_count=site_count)

@app.route('/sites')
@login_required
def sites():
    pass


@app.route('/scantypes')
@login_required
def scantypes():
    pass

@app.route('/users')
@login_required
def users():
    """
    View that lists all users
    """
    if not current_user.is_admin:
        flash('You are not authorised')
        return redirect(url_for('user'))
    users = User.query.all()
    user_forms = []
    for user in users:
        form = UserForm()
        form.user_id.data = user.id
        form.realname.data = user.realname
        form.is_admin.data = user.is_admin
        form.has_phi.data = user.has_phi
        study_ids = [str(study.id) for study in user.studies]
        form.studies.data = study_ids
        user_forms.append(form)
    return render_template('users.html',
                           studies=current_user.get_studies(),
                           user_forms=user_forms)

@app.route('/user', methods=['GET', 'POST'])
@app.route('/user/<int:user_id>', methods=['GET', 'POST'])
@login_required
def user(user_id=None):
    """
    View for updating a user's information
    """

    if user_id and user_id != current_user.id and not current_user.dashboard_admin:
        flash("You are not authorized to view other user settings")
        return redirect(url_for('user'))

    if user_id:
        user = User.query.get(user_id)
    else:
        user = current_user

    form = get_user_form(user, current_user)

    if form.validate_on_submit():
        submitted_id = form.id.data

        if submitted_id != current_user.id and not current_user.dashboard_admin:
            # This if statement is needed to catch malicious users who update
            # the user_id submitted with the form to modify other user settings
            flash("You are not authorized to update other users' settings.")
            return redirect(url_for('user'))

        updated_user = User.query.get(submitted_id)

        if form.update_access.data:
            # Give user access to new study
            for study in form.add_access.data:
                record = StudyUser(study, updated_user.id)
                updated_user.studies.append(record)
        elif form.revoke_all_access.data:
            # Revoke access to all enabled studies
            for study_user_record in updated_user.studies:
                db.session.delete(study_user_record)
        else:
            # Update user info
            form.populate_obj(updated_user)

        # Revoke user's access to a single study if a 'remove access' was pushed
        for study_form in form.studies:
            if not study_form.revoke_access.data:
                continue
            for study_user_record in updated_user.studies:
                if study_user_record.study_id == study_form.study_id.data:
                    db.session.delete(study_user_record)
            # Only one can be pushed at a time, so exit once the match is deleted
            break

        db.session.add(updated_user)
        db.session.commit()

        flash("User profile updated.")
        return redirect(url_for('user', user_id=submitted_id))

    report_form_errors(form)

    return render_template('user.html', user=user, form=form)

@app.route('/session_by_name')
@app.route('/session_by_name/<session_name>', methods=['GET'])
@login_required
def session_by_name(session_name=None):
    """
    Basically just a helper view, converts a session name to a sesssion_id
    and returns the session view
    """
    if session_name is None:
        return redirect('/index')
    # Strip any file extension or qc_ prefix
    session_name = session_name.replace('qc_', '')
    session_name = os.path.splitext(session_name)[0]

    q = Session.query.filter(Session.name == session_name)
    if q.count() < 1:
        flash('Session not found')
        return redirect(url_for('index'))

    session = q.first()

    if not current_user.has_study_access(session.study):
        flash('Not authorised')
        return redirect(url_for('index'))

    return redirect(url_for('session', session_id=session.id))


@app.route('/create_issue/<string:session_id>', methods=['GET', 'POST'])
@app.route('/create_issue/<string:session_id>/<issue_title>/<issue_body>', methods=['GET', 'POST'])
@fresh_login_required
def create_issue(session_id, issue_title="", issue_body=""):
    """
    Post a session issue to github, returns to the session view
    """
    session = Session.query.get(session_id)
    if not current_user.has_study_access(session.study):
        flash("Not authorised")
        return redirect(url_for('index'))

    token = flask_session['active_token']

    if issue_title and issue_body:
        try:
            gh = Github(token)
            repo = gh.get_user("TIGRLab").get_repo("Admin")
            iss = repo.create_issue(issue_title, issue_body)
            session.gh_issue = iss.number
            db.session.commit()
            flash("Issue '{}' created!".format(issue_title))
        except:
            flash("Issue '{}' was not created successfully.".format(issue_title))
    else:
        flash("Please enter both an issue title and description.")
    return(redirect(url_for('session', session_id=session.id)))

############## Timepoint view functions ########################################
# timepoint() is the main view and renders the html users interact with.
#
# The other routes all handle different button clicks from the timepoint() view
# and then immediately redirect back to it.

@app.route('/study/<string:study_id>/timepoint/<string:timepoint_id>',
        methods=['GET', 'POST'])
@login_required
def timepoint(study_id, timepoint_id, delete=False, flag_finding=False):
    """
    Default view for a single timepoint.
    """
    timepoint = get_timepoint(study_id, timepoint_id, current_user)
    empty_form = EmptySessionForm()
    findings_form = IncidentalFindingsForm()
    comments_form = TimepointCommentsForm()
    return render_template('timepoint/main.html', study_id=study_id,
            timepoint=timepoint, empty_session_form=empty_form,
            incidental_findings_form=findings_form,
            timepoint_comments_form=comments_form)

@app.route('/study/<string:study_id>/timepoint/<string:timepoint_id>' + \
        '/sign_off/<int:session_num>', methods=['GET', 'POST'])
@login_required
def sign_off(study_id, timepoint_id, session_num):
    timepoint = get_timepoint(study_id, timepoint_id, current_user)
    dest_URL = url_for('timepoint', study_id=study_id,
            timepoint_id=timepoint_id)
    session = get_session(timepoint, session_num, dest_URL)
    session.sign_off(current_user.id)
    flash("{} review completed.".format(session))
    return redirect(dest_URL)

@app.route('/study/<string:study_id>/timepoint/<string:timepoint_id>' + \
        '/add_comment', methods=['POST'])
@app.route('/study/<string:study_id>/timepoint/<string:timepoint_id>' + \
        '/add_comment/<int:comment_id>', methods=['POST', 'GET'])
@login_required
def add_comment(study_id, timepoint_id, comment_id=None):
    timepoint = get_timepoint(study_id, timepoint_id, current_user)
    dest_URL = url_for('timepoint', study_id=study_id,
            timepoint_id=timepoint_id)

    form = TimepointCommentsForm()
    if form.validate_on_submit():
        if comment_id:
            try:
                timepoint.update_comment(current_user.id, comment_id,
                        form.comment.data)
            except Exception as e:
                flash("Failed to update comment. Reason: {}".format(e))
            else:
                flash("Updated comment.")
            return redirect(dest_URL)
        comment = timepoint.add_comment(current_user.id, form.comment.data)
    return redirect(dest_URL)

@app.route('/study/<string:study_id>/timepoint/<string:timepoint_id>' + \
        '/delete_comment/<int:comment_id>', methods=['GET'])
@study_admin_required
@login_required
def delete_comment(study_id, timepoint_id, comment_id):
    timepoint = get_timepoint(study_id, timepoint_id, current_user)
    dest_URL = url_for('timepoint', study_id=study_id,
            timepoint_id=timepoint_id)
    try:
        timepoint.delete_comment(comment_id)
    except Exception as e:
        flash("Failed to delete comment. {}".format(e))
    return redirect(dest_URL)

@app.route('/study/<string:study_id>/timepoint/<string:timepoint_id>' + \
        '/flag_finding', methods=['POST'])
@login_required
def flag_finding(study_id, timepoint_id):
    timepoint = get_timepoint(study_id, timepoint_id, current_user)
    dest_URL = url_for('timepoint', study_id=study_id,
            timepoint_id=timepoint_id)

    form = IncidentalFindingsForm()
    if form.validate_on_submit():
        timepoint.report_incidental_finding(current_user.id, form.comment.data)
        incidental_finding_email(current_user, timepoint.name, form.comment.data)
        flash("Report submitted.")
    return redirect(dest_URL)

@app.route('/study/<string:study_id>/timepoint/<string:timepoint_id>' + \
        '/delete', methods=['GET'])
@study_admin_required
@login_required
def delete_timepoint(study_id, timepoint_id):
    timepoint = get_timepoint(study_id, timepoint_id, current_user)
    timepoint.delete()
    flash("{} has been deleted.".format(timepoint))
    return redirect(url_for('study', study_id=study_id))

@app.route('/study/<string:study_id>/timepoint/<string:timepoint_id>' + \
        '/delete_session/<int:session_num>', methods=['GET'])
@study_admin_required
@login_required
def delete_session(study_id, timepoint_id, session_num):
    timepoint = get_timepoint(study_id, timepoint_id, current_user)
    dest_URL = url_for('timepoint', study_id=study_id,
            timepoint_id=timepoint_id)
    session = get_session(timepoint, session_num, dest_URL)
    session.delete()
    flash("{} has been deleted.".format(session))
    return redirect(dest_URL)

@app.route('/study/<string:study_id>/timepoint/<string:timepoint_id>' + \
        '/dismiss_redcap/<int:session_num>', methods=['GET', 'POST'])
@study_admin_required
@login_required
def dismiss_redcap(study_id, timepoint_id, session_num):
    """
    Dismiss a session's 'missing redcap' error message.
    """
    timepoint = get_timepoint(study_id, timepoint_id, current_user)
    dest_URL = url_for('timepoint', study_id=study_id,
            timepoint_id=timepoint_id)
    session = get_session(timepoint, session_num, dest_URL)
    timepoint.dismiss_redcap_error(session_num)
    flash("Successfully updated.")
    return redirect(dest_URL)

@app.route('/study/<string:study_id>/timepoint/<string:timepoint_id>' + \
        '/dismiss_missing/<int:session_num>', methods=['POST'])
@study_admin_required
@login_required
def dismiss_missing(study_id, timepoint_id, session_num):
    """
    Dismiss a session's 'missing scans' error message
    """
    timepoint = get_timepoint(study_id, timepoint_id, current_user)
    dest_URL = url_for('timepoint', study_id=study_id,
            timepoint_id=timepoint_id)
    session = get_session(timepoint, session_num, dest_URL)

    form = EmptySessionForm()
    if form.validate_on_submit():
        timepoint.ignore_missing_scans(session_num, current_user.id,
                form.comment.data)
        flash("Succesfully updated.")

    return redirect(dest_URL)

############## End of Timepoint View functions #################################

@app.route('/redcap_redirect/<int:session_id>', methods=['GET'])
@login_required
def redcap_redirect(session_id):
    """
    Used to provide a link from the session page to a redcap session complete record
    """
    session = Session.query.get(session_id)
    if session.redcap_eventid:
        redcap_url = '{}redcap_v{}/DataEntry/index.php?pid={}&id={}&event_id={}&page={}'
        redcap_url = redcap_url.format(session.redcap_url,
                                       session.redcap_version,
                                       session.redcap_projectid,
                                       session.redcap_record,
                                       session.redcap_eventid,
                                       session.redcap_instrument)
    else:
        redcap_url = '{}redcap_v{}/DataEntry/index.php?pid={}&id={}&page={}'
        redcap_url = redcap_url.format(session.redcap_url,
                                       session.redcap_version,
                                       session.redcap_projectid,
                                       session.redcap_record,
                                       session.redcap_instrument)
    return(redirect(redcap_url))

@app.route('/scan', methods=["GET"])
@app.route('/scan/<int:scan_id>', methods=['GET', 'POST'])
@login_required
def scan(scan_id=None):
    """
    Default view for a single scan
    This object actually takes an id from the session_scans table and uses
    that to identify the scan and session
    """

    if scan_id is None:
        flash('Invalid scan')
        return redirect(url_for('index'))

    # Check the user has permission to see this study
    studies = current_user.get_studies()
    session_scan = Session_Scan.query.get(session_scan_id)
    scan = session_scan.scan
    session = session_scan.session
    if not current_user.has_study_access(session.study):
        flash('Not authorised')
        return redirect(url_for('index'))

    # form for updating the study blacklist.csv on the filesystem
    bl_form = ScanBlacklistForm()
    # form used for updating the analysis comments
    scancomment_form = ScanCommentForm()

    if not bl_form.is_submitted():
        # this isn't an update so just populate the blacklist form with current values from the database
        # these should be the same as in the filesystem
        bl_form.scan_id = scan.id
        bl_form.bl_comment.data = scan.bl_comment

    if bl_form.validate_on_submit():
        # going to make an update to the blacklist
        # update the scan object in the database with info from the form
        # updating the databse object automatically syncronises blacklist.csv on the filesystem
        #   see models.py
        if bl_form.delete.data:
            scan.bl_comment = None
        else:
            scan.bl_comment = bl_form.bl_comment.data

        try:
            db.session.add(scan)
            db.session.commit()
            flash("Blacklist updated")
            return redirect(url_for('session', session_id=session.id))
        except SQLAlchemyError as err:
            logger.error('Scan blacklist update failed:{}'.format(str(err)))
            flash('Update failed, admins have been notified, please try again')


    return render_template('scan.html',
                           studies=studies,
                           scan=scan,
                           session=session,
                           session_scan=session_scan,
                           blacklist_form=bl_form,
                           scancomment_form=scancomment_form)


@app.route('/study/<string:study_id>', methods=['GET', 'POST'])
@app.route('/study/<string:study_id>/<active_tab>', methods=['GET', 'POST'])
@login_required
def study(study_id=None, active_tab=None):
    """
    This is the main view for a single study.
    The page is a tabulated view, I would have done this differently given
    another chance.
    """
    if not current_user.has_study_access(study_id):
        flash('Not authorised')
        return redirect(url_for('index'))

    # this is used to update the readme text file
    form = StudyOverviewForm()

    # load the study config
    cfg = dm.config.config()
    try:
        cfg.set_study(study_id)
    except KeyError:
        abort(500)

    # Get the contents of the study README.md file from the file system
    readme_path = os.path.join(cfg.get_study_base(), 'README.md')

    try:
        with codecs.open(readme_path, encoding='utf-8', mode='r') as myfile:
            data = myfile.read()
    except IOError:
        data = ''

    if form.validate_on_submit():
        # form has been submitted check for changes
        # simple MD seems to replace \n with \r\n
        form.readme_txt.data = form.readme_txt.data.replace('\r', '')

        # also strip blank lines at the start and end as these are
        # automatically stripped when the form is submitted
        if not form.readme_txt.data.strip() == data.strip():
            if os.path.exists(readme_path):
                # form has been updated so make a backup and write back to file
                timestamp = datetime.datetime.now().strftime('%Y%m%d%H%m')
                base, ext = os.path.splitext(readme_path)
                backup_file = base + '_' + timestamp + ext
                try:
                    shutil.copyfile(readme_path, backup_file)
                except (IOError, os.error, shutil.Error), why:
                    logger.error('Failed to backup readme for study {} with excuse {}'
                                 .format(study_id, why))
                    abort(500)

            with codecs.open(readme_path, encoding='utf-8', mode='w') as myfile:
                myfile.write(form.readme_txt.data)
            data = form.readme_txt.data

    form.readme_txt.data = data
    form.study_id.data = study_id

    # get the list of metrics to be displayed in the graph pages from the study config
    display_metrics = app.config['DISPLAY_METRICS']

    # get the study object from the database
    study = Study.query.get(study_id)

    return render_template('study.html',
                           metricnames=study.get_valid_metric_names(),
                           study=study,
                           form=form,
                           active_tab=active_tab,
                           display_metrics=display_metrics)


@app.route('/person')
@app.route('/person/<int:person_id>', methods=['GET'])
@login_required
def person(person_id=None):
    """
    Place holder, does nothing
    """
    return redirect('/index')


@app.route('/metricData', methods=['GET', 'POST'])
@login_required
def metricData():
    """
    This is a generic view for querying the database
    It allows selection of any combination of metrics and returns the data
    in csv format suitable for copy / pasting into a spreadsheet

    The form is submitted on any change, this allows dynamic updating of the
    selection boxes displayed in the html (i.e. is SPINS study is selected
    the sites selector is filtered to only show sites relevent to SPINS).

    We only actually query and return the data if the query_complete is defined
    as true - this is done by client-side javascript.

    A lot of this could have been done client side but I think we all prefer
    writing python to javascript.
    """
    form = SelectMetricsForm()
    data = None
    csv_data = None
    csvname = 'dashboard/output.csv'
    w_file= open(csvname, 'w')


    if form.query_complete.data == 'True':
        data = metricDataAsJson()
        # convert the json object to csv format
        # Need the data field of the response object (data.data)
        temp_data = json.loads(data.data)["data"]
        if temp_data:


            testwrite = csv.writer(w_file)

            csv_data = io.BytesIO()
            csvwriter = csv.writer(csv_data)
            csvwriter.writerow(temp_data[0].keys())
            testwrite.writerow(temp_data[0].keys())
            for row in temp_data:
                csvwriter.writerow(row.values())
                testwrite.writerow(row.values())

    w_file.close()

    # anything below here is for making the form boxes dynamic
    if any([form.study_id.data,
            form.site_id.data,
            form.scantype_id.data,
            form.metrictype_id.data]):
        form_vals = query_metric_types(studies=form.study_id.data,
                                       sites=form.site_id.data,
                                       scantypes=form.scantype_id.data,
                                       metrictypes=form.metrictype_id.data)

    else:
        form_vals = query_metric_types()

    study_vals = []
    site_vals = []
    scantype_vals = []
    metrictype_vals = []

    for res in form_vals:
        study_vals.append((res[0].id, res[0].name))
        site_vals.append((res[1].id, res[1].name))
        scantype_vals.append((res[2].id, res[2].name))
        metrictype_vals.append((res[3].id, res[3].name))
        # study_vals.append((res.id, res.name))
        # for site in res.sites:
        #     site_vals.append((site.id, site.name))
        # for scantype in res.scantypes:
        #     scantype_vals.append((scantype.id, scantype.name))
        #     for metrictype in scantype.metrictypes:
        #         metrictype_vals.append((metrictype.id, metrictype.name))

    #sort the values alphabetically
    study_vals = sorted(set(study_vals), key=lambda v: v[1])
    site_vals = sorted(set(site_vals), key=lambda v: v[1])
    scantype_vals = sorted(set(scantype_vals), key=lambda v: v[1])
    metrictype_vals = sorted(set(metrictype_vals), key=lambda v: v[1])

    flask_session['study_name'] = study_vals
    flask_session['site_name'] = site_vals
    flask_session['scantypes_name'] = scantype_vals
    flask_session['metrictypes_name'] = metrictype_vals


    # this bit actually updates the valid selections on the form
    form.study_id.choices = study_vals
    form.site_id.choices = site_vals
    form.scantype_id.choices = scantype_vals
    form.metrictype_id.choices = metrictype_vals

    reader = csv.reader(open(csvname, 'r'))
    csvList = [row for row in reader]

    if csv_data:
        csv_data.seek(0)
        return render_template('getMetricData.html', form=form, data=csvList)
    else:
        return render_template('getMetricData.html', form=form, data="")

def _checkRequest(request, key):
    # Checks a post request, returns none if key doesn't exist
    try:
        return(request.form.getlist(key))
    except KeyError:
        return(None)

@app.route('/DownloadCSV')
@login_required
def downloadCSV():

    output = make_response(send_file('output.csv', as_attachment=True))
    output.headers["Content-Disposition"] = "attachment; filename=output.csv"
    output.headers["Content-type"] = "text/csv"
    output.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    output.headers['Pragma'] = 'no-cache'
    return output


@app.route('/metricDataAsJson', methods=['Get', 'Post'])
@login_required
def metricDataAsJson(format='http'):
    """
    Query the database for metric database, handles both GET (generated by client side javascript for creating graphs)
    and POST (generated by the metricData form view) requests

    Can be called directly:
       http://srv-dashboard.camhres.ca/metricDataAsJson
    This will return all data in the database (probs not what you want).

    Filters can be defined in the http request object
        (http://flask.pocoo.org/docs/0.12/api/#incoming-request-data)
        this is a global flask object that is automatically created whenever a URL is
        requested.
        e.g.:
        http://srv-dashboard.camhres.ca/metricDataAsJson?studies=15&sessions=1739,1744&metrictypes=84
        creates a request object
            request.args = {studies: 15,
                            sessions: [1739, 1744],
                            metrictypes: 84}
    If byname is defined (and evaluates True) in the request.args then filters
       can be defined by name instead of by database id
    e.g. http://srv-dashboard.camhres.ca/metricDataAsJson?byname=True&studies=ANDT&sessions=ANDT_CMH_101_01&metrictypes=modulename

    Function works slightly differently if the request method is POST
    (such as that generated by metricData())
        In that case the field names are expected to be the primary keys from the database
        as these are used to create the form.
    """
    # Define the mapping from GET field names to POST field names
    # it's going to get replaced either by the requested values (if defined in the request object)
    # or by None if not set as a filter
    fields = {'studies': 'study_id',
              'sites': 'site_id',
              'sessions': 'session_id',
              'scans': 'scan_id',
              'scantypes': 'scantype_id',
              'metrictypes': 'metrictype_id'}

    byname = False # switcher to allow getting values byname instead of id

    try:
        if request.method == 'POST':
            byname = request.form['byname']
        else:
            byname = request.args.get('byname')
    except KeyError:
        pass

    # extract the values from the request object and populate
    for k, v in fields.iteritems():
        if request.method == 'POST':
            fields[k] = _checkRequest(request, v)
        else:
            if request.args.get(k):
                fields[k] = [x.strip() for x in request.args.get(k).split(',')]
            else:
                fields[k] = None

    # remove None values from the dict
    fields = dict((k, v) for k, v in fields.iteritems() if v)

    # make the database query
    if byname:
        data = query_metric_values_byname(**fields)
    else:
        # convert from strings to integers
        for k, vals in fields.iteritems():
            try:
                fields[k] = int(vals)
            except TypeError:
                fields[k] = [int(v) for v in vals]

        data = query_metric_values_byid(**fields)

    # the database query returned a list of sqlachemy record objects.
    # convert these into a standard list of dicts so we can jsonify it
    objects = []


    for metricValue in data:

        string_row = str(metricValue)

        session = [session_link.session for session_link in metricValue.scan.sessions
                   if session_link.is_primary][0]
        objects.append({'value':            metricValue.value,
                        'metrictype':       metricValue.metrictype.name,
                        'metrictype_id':    metricValue.metrictype_id,
                        'scan_id':          metricValue.scan_id,
                        'scan_name':        metricValue.scan.name,
                        'scan_description': metricValue.scan.description,
                        'scantype':         metricValue.scan.scantype.name,
                        'scantype_id':      metricValue.scan.scantype_id,
                        'session_id':       session.id,
                        'session_name':     session.name,
                        # 'session_date':     metricValue.scan.session.date,
                        'site_id':          session.site_id,
                        'site_name':        session.site.name,
                        'study_id':         session.study_id,
                        'study_name':       session.study.name})

    if format == 'http':
        # spit this out in a format suitable for client side processing
        return(jsonify({'data': objects}))
    else:
        # return a pretty object for human readable
        return(json.dumps(objects, indent=4, separators=(',', ': ')))


@app.route('/todo')
@app.route('/todo/<int:study_id>', methods=['GET'])
@login_required
def todo(study_id=None):
    """
    Runs the datman binary dm-qc-todo.py and returns the results
    as a json object
    """
    if study_id:
        study = Study.query.get(study_id)
        study_name = study.nickname
    else:
        study_name = None

    if not current_user.has_study_access(study_id):
        flash('Not authorised')
        return redirect(url_for('index'))

    # todo_list = utils.get_todo(study_name)
    try:
        todo_list = utils.get_todo(study_name)
    except utils.TimeoutError:
        # should do something nicer here
        todo_list = {'error': 'timeout'}
    except RuntimeError as e:
        todo_list = {'error': 'runtime:{}'.format(e)}
    except:
        todo_list = {'error': 'other'}

    return jsonify(todo_list)


@app.route('/redcap', methods=['GET', 'POST'])
def redcap():
    """
    Route used by the redcap ScanCompeted data callback.
    Basically grabs the redcap record id and updates the database.
    """
    logger.info('Recieved a query from redcap')
    if request.method == 'POST':
        logger.debug('Recieved keys:{} from REDcap.'.format(request.form.keys()))
        logger.debug(request.form['project_url'])
        try:
            rc = REDCAP.redcap_record(request)
        except Exception as e:
            logger.error('Failed creating redcap object:{}'.format(str(e)))
            logger.debug(str(e))
            raise InvalidUsage(str(e), status_code=400)
    else:
        logger.error('Invalid redcap response')
        raise InvalidUsage('Expected a POST request', status_code=400)
    logger.info('Processing query')
    if rc.instrument_completed:
        logger.info('Updating db session.')
        rc.update_db_session()
    else:
        logger.info('Instrument not complete, ignoring.')

    rc.get_survey_return_code()

    return render_template('200.html'), 200


@app.errorhandler(404)
def not_found_error(error):
    return render_template('404.html'), 404


@app.errorhandler(500)
def internal_error(error):
    db.session.rollback()
    return render_template('500.html'), 500


@app.route('/login')
def login():
    return render_template('login.html')


@app.route('/authorize/<provider>')
def oauth_authorize(provider):
    if not current_user.is_anonymous:
        return redirect(url_for('login'))
    oauth = OAuthSignIn.get_provider(provider)
    return oauth.authorize()


@app.route('/callback/<provider>')
def oauth_callback(provider):
    if not current_user.is_anonymous:
        return redirect(url_for('index'))
    oauth = OAuthSignIn.get_provider(provider)
    access_token, user_info = oauth.callback()

    if access_token is None:
        flash('Authentication failed.')
        return redirect(url_for('login'))

    if provider == 'github':
        username = user_info['login']
        user = User.query.filter_by(github_name=username).first()
    elif provider == 'gitlab':
        username = user_info['username']
        user = User.query.filter_by(gitlab_name=username).first()

    if not user:
        # Temp. disabled. Need to update account creation process to avoid empty name / email fields
        redirect(404)
        # username = User.make_unique_nickname(username)
        # user = User(username=username,
        #             realname=github_user['name'],
        #             email=github_user['email'])
        # db.session.add(user)
        # db.session.commit()

    login_user(user, remember=True)
    # Not sure why this was done manually so commenting out for now
    flask_session['active_token'] = access_token
    return redirect(url_for('index'))

@app.route('/scan_comment', methods=['GET','POST'])
@app.route('/scan_comment/<scan_link_id>', methods=['GET','POST'])
@login_required
def scan_comment(scan_link_id):
    """
    View for adding scan comments
    Comments are specific to a scan and can be seen by all
    studies linking to that scan.
    scan_link_id is passed for security checking and to ensure we get the user
    back to the right place.

    """
    scan_link = Session_Scan.query.get(scan_link_id)
    scan = scan_link.scan
    session = scan_link.session

    form = ScanCommentForm()
    form.user_id = current_user.id
    form.scan_id = scan.id

    if not current_user.has_study_access(session.study):
        flash('Not authorised')
        return redirect(url_for('index'))

    if form.validate_on_submit():
        try:
            scancomment = ScanComment()
            scancomment.scan_id = scan.id
            scancomment.user_id = current_user.id
            scancomment.analysis_id = form.analyses.data
            scancomment.excluded = form.excluded.data
            scancomment.comment = form.comment.data

            db.session.add(scancomment)
            db.session.commit()
            flash('Scan comment added')
        except Exception as e:
            assert app.debug==False
            flash('Failed adding comment')

    return redirect(url_for('session', session_id=session.id))


@app.route('/scan_blacklist', methods=['GET','POST'])
@app.route('/scan_blacklist/<scan_id>', methods=['GET','POST'])
@login_required
def scan_blacklist(scan_id):
    """
    View for adding scan comments
    """
    form = ScanBlacklistForm()
    form.scan_id = scan_id

    scan = Scan.query.get(scan_id)
    session = scan.session

    if not current_user.has_study_access(session.study):
        flash('Not authorised')
        return redirect(url_for('index'))

    if form.validate_on_submit():
        try:
            scan.bl_comment = form.bl_comment.data

            db.session.commit()
            flash('Scan blacklisted')
        except:
            flash('Failed blacklisting scan')

    return redirect(url_for('session', session_id=session.id))


@app.route('/analysis', methods=['GET','POST'])
@app.route('/analysis/<analysis_id>')
@login_required
def analysis(analysis_id=None):
    """
    Default view for analysis
    """
    form = AnalysisForm()
    if form.validate_on_submit():
        try:
            analysis = Analysis()
            analysis.name = form.name.data
            analysis.description = form.description.data
            analysis.software = form.software.data

            db.session.add(analysis)
            db.session.commit()
            flash('Analysis added')
        except:
            flash('Failed adding analysis')

    if not analysis_id:
        analyses = Analysis.query.all()
    else:
        analyses = Analysis.query.get(analysis_id)

    for analysis in analyses:
        # format the user objects for display on page
        users = analysis.get_users()
        analysis.user_names = ' '.join([user.realname for user in users])

    return render_template('analyses.html',
                           analyses=analyses,
                           form=form)
