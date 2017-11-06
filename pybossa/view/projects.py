# -*- coding: utf8 -*-
# This file is part of PYBOSSA.
#
# Copyright (C) 2017 Scifabric LTD.
#
# PYBOSSA is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# PYBOSSA is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with PYBOSSA.  If not, see <http://www.gnu.org/licenses/>.

import time
import re
import json
import os
import math
import requests
from StringIO import StringIO

from flask import Blueprint, request, url_for, flash, redirect, abort, Response, current_app
from flask import render_template, make_response, session
from flask import Markup, jsonify
from flask.ext.login import login_required, current_user
from flask.ext.babel import gettext
from flask_wtf.csrf import generate_csrf
from rq import Queue

import pybossa.sched as sched

from pybossa.core import (uploader, signer, sentinel, json_exporter,
                          csv_exporter, importer, db,
                          task_json_exporter, task_csv_exporter,
                          project_csv_exporter, project_report_csv_exporter)
from pybossa.model import make_uuid
from pybossa.model.project import Project
from pybossa.model.category import Category
from pybossa.model.task import Task
from pybossa.model.task_run import TaskRun
from pybossa.model.auditlog import Auditlog
from pybossa.model.project_stats import ProjectStats
from pybossa.model.webhook import Webhook
from pybossa.model.blogpost import Blogpost
from pybossa.util import (Pagination, admin_required, get_user_id_or_ip, rank,
                          handle_content_type, redirect_content_type,
                          get_avatar_url, admin_or_subadmin_required, AttrDict)
from pybossa.auth import ensure_authorized_to
from pybossa.cache import projects as cached_projects
from pybossa.cache import users as cached_users
from pybossa.cache import categories as cached_cat
from pybossa.cache import project_stats as stats
from pybossa.cache.helpers import add_custom_contrib_button_to, has_no_presenter
from pybossa.cache.task_browse_helpers import (get_searchable_columns,
                                               parse_tasks_browse_args)
from pybossa.ckan import Ckan
from pybossa.extensions import misaka
from pybossa.cookies import CookieHandler
from pybossa.password_manager import ProjectPasswdManager
from pybossa.jobs import (webhook, send_mail,
                          import_tasks, IMPORT_TASKS_TIMEOUT,
                          delete_bulk_tasks, TASK_DELETE_TIMEOUT,
                          export_tasks, EXPORT_TASKS_TIMEOUT)
from pybossa.forms.projects_view_forms import *
from pybossa.importers import BulkImportException
from pybossa.pro_features import ProFeatureHandler

from pybossa.core import (project_repo, user_repo, task_repo, blog_repo,
                          result_repo, webhook_repo, auditlog_repo)
from pybossa.auditlogger import AuditLogger
from pybossa.contributions_guard import ContributionsGuard
from pybossa.default_settings import TIMEOUT
from pybossa.forms.admin_view_forms import *
from pybossa.cache.helpers import n_available_tasks, oldest_available_task, n_completed_tasks_by_user
from pybossa.cache.helpers import n_available_tasks_for_user, latest_submission_task_date
from pybossa.util import crossdomain
from pybossa.error import ErrorStatus
from pybossa.syncer import NotEnabled
from pybossa.syncer.project_syncer import ProjectSyncer

cors_headers = ['Content-Type', 'Authorization']


blueprint = Blueprint('project', __name__)

MAX_NUM_SYNCHRONOUS_TASKS_IMPORT = 200
MAX_NUM_SYNCHRONOUS_TASKS_DELETE = 1000
DEFAULT_TASK_TIMEOUT = ContributionsGuard.STAMP_TTL

auditlogger = AuditLogger(auditlog_repo, caller='web')
mail_queue = Queue('email', connection=sentinel.master)
importer_queue = Queue('medium',
                       connection=sentinel.master,
                       default_timeout=IMPORT_TASKS_TIMEOUT)
webhook_queue = Queue('high', connection=sentinel.master)
task_queue = Queue('medium',
                   connection=sentinel.master,
                   default_timeout=TASK_DELETE_TIMEOUT)
export_queue = Queue('low',
                     connection=sentinel.master,
                     default_timeout=EXPORT_TASKS_TIMEOUT)


def sanitize_project_owner(project, owner, current_user, ps=None):
    """Sanitize project and owner data."""
    if current_user.is_authenticated() and owner.id == current_user.id:
        if isinstance(project, Project):
            project_sanitized = project.dictize()   # Project object
        else:
            project_sanitized = project             # dict object
        owner_sanitized = cached_users.get_user_summary(owner.name)
    else:   # anonymous or different owner
        if request.headers.get('Content-Type') == 'application/json':
            if isinstance(project, Project):
                project_sanitized = project.to_public_json()            # Project object
            else:
                project_sanitized = Project().to_public_json(project)   # dict object
        else:    # HTML
            # Also dictize for HTML to have same output as authenticated user (see above)
            if isinstance(project, Project):
                project_sanitized = project.dictize()   # Project object
            else:
                project_sanitized = project             # dict object
        owner_sanitized = cached_users.public_get_user_summary(owner.name)
    if ps:
        project_sanitized['n_tasks'] = ps.n_tasks
        project_sanitized['n_task_runs'] = ps.n_tasks
        project_sanitized['n_results'] = ps.n_results
        project_sanitized['n_completed_tasks'] = ps.n_completed_tasks
        project_sanitized['n_volunteers'] = ps.n_volunteers
        project_sanitized['overall_progress'] = ps.overall_progress
        project_sanitized['n_blogposts'] = ps.n_blogposts
        project_sanitized['last_activity'] = ps.last_activity
        project_sanitized['overall_progress'] = ps.overall_progress
    return project_sanitized, owner_sanitized


def project_title(project, page_name):
    if not project:  # pragma: no cover
        return "Project not found"
    if page_name is None:
        return "Project: %s" % (project.name)
    return "Project: %s &middot; %s" % (project.name, page_name)


def project_by_shortname(short_name):
    project = cached_projects.get_project(short_name)
    if project:
        # Get owner
        ps = stats.get_stats(project.id, full=True)
        owner = user_repo.get(project.owner_id)
        return (project, owner, ps)
    else:
        cached_projects.delete_project(short_name)
        return abort(404)


def allow_deny_project_info(project_short_name):
    """Return project info for user as admin, subadmin or project coowner."""
    project, owner, ps = project_by_shortname(project_short_name)
    if not current_user.admin and not current_user.subadmin \
            and not current_user.id in project.owners_ids:
        return abort(403)
    return project, owner, ps


def pro_features(owner=None):
    feature_handler = ProFeatureHandler(current_app.config.get('PRO_FEATURES'))
    pro = {
        'auditlog_enabled': feature_handler.auditlog_enabled_for(current_user),
        'autoimporter_enabled': feature_handler.autoimporter_enabled_for(current_user),
        'webhooks_enabled': feature_handler.webhooks_enabled_for(current_user)
    }
    if owner:
        pro['better_stats_enabled'] = feature_handler.better_stats_enabled_for(
                                          current_user,
                                          owner)
    return pro


@blueprint.route('/category/featured/', defaults={'page': 1})
@blueprint.route('/category/featured/page/<int:page>/')
def index(page):
    """List projects in the system"""
    if cached_projects.n_count('featured') > 0:
        return project_index(page, cached_projects.get_all_featured,
                             'featured',
                             True, False)
    else:
        categories = cached_cat.get_all()
        cat_short_name = categories[0].short_name
        return redirect_content_type(url_for('.project_cat_index', category=cat_short_name))


def project_index(page, lookup, category, fallback, use_count):
    """Show projects of a category"""

    per_page = current_app.config['APPS_PER_PAGE']

    ranked_projects = rank(lookup(category))
    offset = (page - 1) * per_page
    projects = ranked_projects[offset:offset+per_page]

    count = cached_projects.n_count(category)

    data = []

    if fallback and not projects:  # pragma: no cover
        return redirect(url_for('.index'))

    pagination = Pagination(page, per_page, count)
    categories = cached_cat.get_all()
    # Check for pre-defined categories featured and draft
    featured_cat = Category(name='Featured',
                            short_name='featured',
                            description='Featured projects')
    if category == 'featured':
        active_cat = featured_cat
    elif category == 'draft':
        active_cat = Category(name='Draft',
                              short_name='draft',
                              description='Draft projects')
    else:
        active_cat = project_repo.get_category_by(short_name=category)

    # Check if we have to add the section Featured to local nav
    if cached_projects.n_count('featured') > 0:
        categories.insert(0, featured_cat)
    template_args = {
        "projects": projects,
        "title": gettext("Projects"),
        "pagination": pagination,
        "active_cat": active_cat,
        "categories": categories,
        "template": '/projects/index.html'}

    if use_count:
        template_args.update({"count": count})
    return handle_content_type(template_args)


@blueprint.route('/category/draft/', defaults={'page': 1})
@blueprint.route('/category/draft/page/<int:page>/')
@login_required
@admin_required
def draft(page):
    """Show the Draft projects"""
    return project_index(page, cached_projects.get_all_draft, 'draft',
                     False, True)


@blueprint.route('/category/<string:category>/', defaults={'page': 1})
@blueprint.route('/category/<string:category>/page/<int:page>/')
@login_required
def project_cat_index(category, page):
    """Show Projects that belong to a given category"""
    return project_index(page, cached_projects.get_all, category, False, True)


@blueprint.route('/new', methods=['GET', 'POST'])
@login_required
@admin_or_subadmin_required
def new():
    ensure_authorized_to('create', Project)
    form = ProjectForm(request.body)

    def respond(errors):
        response = dict(template='projects/new.html',
                        title=gettext("Create a Project"),
                        form=form, errors=errors)
        return handle_content_type(response)

    def _description_from_long_description():
        if form.description.data:
            return form.description.data
        long_desc = form.long_description.data
        html_long_desc = misaka.render(long_desc)[:-1]
        remove_html_tags_regex = re.compile('<[^>]*>')
        blank_space_regex = re.compile('\n')
        text_desc = remove_html_tags_regex.sub("", html_long_desc)[:255]
        if len(text_desc) >= 252:
            text_desc = text_desc[:-3]
            text_desc += "..."
        description = blank_space_regex.sub(" ", text_desc)
        return description if description else " "

    if request.method != 'POST':
        return respond(False)

    if not form.validate():
        flash(gettext('Please correct the errors'), 'error')
        return respond(True)

    info = {'sync': {'enabled': False}}
    category_by_default = cached_cat.get_all()[0]

    project = Project(name=form.name.data,
                      short_name=form.short_name.data,
                      description=_description_from_long_description(),
                      long_description=form.long_description.data,
                      owner_id=current_user.id,
                      info=info,
                      category_id=category_by_default.id,
                      owners_ids=[current_user.id])

    project.set_password(form.password.data)
    project_repo.save(project)

    msg_1 = gettext('Project created!')
    flash(Markup('<i class="icon-ok"></i> {}').format(msg_1), 'success')
    markup = Markup('<i class="icon-bullhorn"></i> {} ' +
                    '<strong><a href="https://docs.pybossa.com"> {}' +
                    '</a></strong> {}')
    flash(markup.format(
              gettext('You can check the '),
              gettext('Guide and Documentation'),
              gettext('for adding tasks, a thumbnail, using PYBOSSA.JS, etc.')),
          'success')
    auditlogger.add_log_entry(None, project, current_user)

    return redirect_content_type(url_for('.update',
                                         short_name=project.short_name))


@blueprint.route('/<short_name>/tasks/taskpresentereditor', methods=['GET', 'POST'])
@login_required
@admin_or_subadmin_required
def task_presenter_editor(short_name):
    errors = False
    project, owner, ps = project_by_shortname(short_name)

    title = project_title(project, "Task Presenter Editor")

    pro = pro_features()

    form = TaskPresenterForm(request.body)
    form.id.data = project.id
    if request.method == 'POST' and form.validate():
        db_project = project_repo.get(project.id)
        old_project = Project(**db_project.dictize())
        old_info = dict(db_project.info)
        old_info['task_presenter'] = form.editor.data

        # Remove GitHub info on save
        for field in ['pusher', 'ref', 'ref_url', 'timestamp']:
            old_info.pop(field, None)

        # Remove sync info on save
        old_sync = old_info.pop('sync', None)
        if old_sync:
            for field in ['syncer', 'latest_sync', 'source_url']:
                old_sync.pop(field, None)
            old_info['sync'] = old_sync

        db_project.info = old_info
        auditlogger.add_log_entry(old_project, db_project, current_user)
        project_repo.update(db_project)
        msg_1 = gettext('Task presenter added!')
        markup = Markup('<i class="icon-ok"></i> {}')
        flash(markup.format(msg_1), 'success')
        return redirect_content_type(url_for('.tasks',
                                             short_name=project.short_name))

    # It does not have a validation
    if request.method == 'POST' and not form.validate():  # pragma: no cover
        flash(gettext('Please correct the errors'), 'error')
        errors = True

    if project.info.get('task_presenter'):
        form.editor.data = project.info['task_presenter']
    else:
        if not request.args.get('template'):
            msg_1 = gettext('<strong>Note</strong> You will need to upload the'
                            ' tasks using the')
            msg_2 = gettext('CSV importer')
            msg_3 = gettext(' or download the project bundle and run the'
                            ' <strong>createTasks.py</strong> script in your'
                            ' computer')
            url = '<a href="%s"> %s</a>' % (url_for('project.import_task',
                                                    short_name=project.short_name), msg_2)
            msg = msg_1 + url + msg_3
            flash(Markup(msg), 'info')

            wrap = lambda i: "projects/presenters/%s.html" % i
            pres_tmpls = map(wrap, current_app.config.get('PRESENTERS'))

            project = add_custom_contrib_button_to(project, get_user_id_or_ip(), ps=ps)
            project_sanitized, owner_sanitized = sanitize_project_owner(project,
                                                                        owner,
                                                                        current_user,
                                                                        ps)
            response = dict(template='projects/task_presenter_options.html',
                            title=title,
                            project=project_sanitized,
                            owner=owner_sanitized,
                            overall_progress=ps.overall_progress,
                            n_tasks=ps.n_tasks,
                            n_task_runs=ps.n_task_runs,
                            last_activity=ps.last_activity,
                            n_completed_tasks=ps.n_completed_tasks,
                            n_volunteers=ps.n_volunteers,
                            presenters=pres_tmpls,
                            pro_features=pro)
            return handle_content_type(response)

        tmpl_uri = "projects/snippets/%s.html" \
            % request.args.get('template')
        tmpl = render_template(tmpl_uri, project=project)
        form.editor.data = tmpl
        msg = 'Your code will be <em>automagically</em> rendered in \
                      the <strong>preview section</strong>. Click in the \
                      preview button!'
        flash(Markup(gettext(msg)), 'info')
    project_sanitized, owner_sanitized = sanitize_project_owner(project,
                                                                owner,
                                                                current_user,
                                                                ps)

    dict_project = add_custom_contrib_button_to(project_sanitized,
                                                get_user_id_or_ip())
    response = dict(template='projects/task_presenter_editor.html',
                    title=title,
                    form=form,
                    project=dict_project,
                    owner=owner_sanitized,
                    overall_progress=ps.overall_progress,
                    n_tasks=ps.n_tasks,
                    n_task_runs=ps.n_task_runs,
                    last_activity=ps.last_activity,
                    n_completed_tasks=ps.n_completed_tasks,
                    n_volunteers=ps.n_volunteers,
                    errors=errors,
                    pro_features=pro)
    return handle_content_type(response)


@blueprint.route('/<short_name>/delete', methods=['GET', 'POST'])
@login_required
def delete(short_name):
    project, owner, ps = project_by_shortname(short_name)

    title = project_title(project, "Delete")
    ensure_authorized_to('read', project)
    ensure_authorized_to('delete', project)
    pro = pro_features()
    project_sanitized, owner_sanitized = sanitize_project_owner(project, owner,
                                                                current_user,
                                                                ps)
    if request.method == 'GET':
        response = dict(template='/projects/delete.html',
                        title=title,
                        project=project_sanitized,
                        owner=owner_sanitized,
                        n_tasks=ps.n_tasks,
                        overall_progress=ps.overall_progress,
                        last_activity=ps.last_activity,
                        pro_features=pro,
                        csrf=generate_csrf())
        return handle_content_type(response)
    project_repo.delete(project)
    auditlogger.add_log_entry(project, None, current_user)
    flash(gettext('Project deleted!'), 'success')
    return redirect_content_type(url_for('account.profile', name=current_user.name))


@blueprint.route('/<short_name>/update', methods=['GET', 'POST'])
@login_required
def update(short_name):
    project, owner, ps = project_by_shortname(short_name)

    def handle_valid_form(form):
        project, owner, ps = project_by_shortname(short_name)

        new_project = project_repo.get_by_shortname(short_name)
        old_project = Project(**new_project.dictize())
        old_info = dict(new_project.info)
        old_project.info = old_info
        if form.id.data == new_project.id:
            new_project.name = form.name.data
            new_project.short_name = form.short_name.data
            new_project.description = form.description.data
            new_project.long_description = form.long_description.data
            new_project.hidden = form.hidden.data
            new_project.webhook = form.webhook.data
            new_project.info = project.info
            new_project.owner_id = project.owner_id
            new_project.allow_anonymous_contributors = form.allow_anonymous_contributors.data
            new_project.category_id = form.category_id.data
            new_project.email_notif = form.email_notif.data

        if form.password.data:
            new_project.set_password(form.password.data)

        sync = new_project.info.get('sync')
        if not sync:
            sync = dict(enabled=form.sync_enabled.data)
        else:
            sync['enabled'] = form.sync_enabled.data
        new_project.info['sync'] = sync

        project_repo.update(new_project)
        auditlogger.add_log_entry(old_project, new_project, current_user)
        cached_cat.reset()
        cached_projects.get_project(new_project.short_name)
        flash(gettext('Project updated!'), 'success')
        return redirect_content_type(url_for('.details',
                                     short_name=new_project.short_name))

    ensure_authorized_to('read', project)
    ensure_authorized_to('update', project)

    pro = pro_features()

    title = project_title(project, "Update")
    if request.method == 'GET':
        sync = project.info.get('sync')
        if sync:
            project.sync_enabled = sync.get('enabled')

        form = ProjectUpdateForm(obj=project)
        upload_form = AvatarUploadForm()
        sync_form = ProjectSyncForm()
        categories = project_repo.get_all_categories()
        form.category_id.choices = [(c.id, c.name) for c in categories]
        if project.category_id is None:
            project.category_id = categories[0].id
        form.populate_obj(project)

    if request.method == 'POST':
        upload_form = AvatarUploadForm()
        sync_form = ProjectSyncForm()
        form = ProjectUpdateForm(request.body)
        categories = cached_cat.get_all()
        form.category_id.choices = [(c.id, c.name) for c in categories]

        if request.form.get('btn') != 'Upload':
            if form.validate():
                return handle_valid_form(form)
            flash(gettext('Please correct the errors'), 'error')
        else:
            if upload_form.validate_on_submit():
                project = project_repo.get(project.id)
                _file = request.files['avatar']
                coordinates = (upload_form.x1.data, upload_form.y1.data,
                               upload_form.x2.data, upload_form.y2.data)
                prefix = time.time()
                _file.filename = "project_%s_thumbnail_%i.png" % (project.id, prefix)
                container = "user_%s" % current_user.id
                uploader.upload_file(_file,
                                     container=container,
                                     coordinates=coordinates)
                # Delete previous avatar from storage
                if project.info.get('thumbnail'):
                    uploader.delete_file(project.info['thumbnail'], container)
                project.info['thumbnail'] = _file.filename
                project.info['container'] = container
                upload_method = current_app.config.get('UPLOAD_METHOD')
                thumbnail_url = get_avatar_url(upload_method,
                                               _file.filename, container)
                project.info['thumbnail_url'] = thumbnail_url
                project_repo.save(project)
                flash(gettext('Your project thumbnail has been updated! It may \
                                  take some minutes to refresh...'), 'success')
            else:
                flash(gettext('You must provide a file to change the avatar'),
                      'error')
            return redirect_content_type(url_for('.update', short_name=short_name))

    project = add_custom_contrib_button_to(project, get_user_id_or_ip(), ps=ps)
    project_sanitized, owner_sanitized = sanitize_project_owner(project, owner,
                                                                current_user,
                                                                ps)
    response = dict(template='/projects/update.html',
                    form=form,
                    upload_form=upload_form,
                    sync_form=sync_form,
                    project=project_sanitized,
                    owner=owner_sanitized,
                    n_tasks=ps.n_tasks,
                    overall_progress=ps.overall_progress,
                    n_task_runs=ps.n_task_runs,
                    last_activity=ps.last_activity,
                    n_completed_tasks=ps.n_completed_tasks,
                    n_volunteers=ps.n_volunteers,
                    title=title,
                    pro_features=pro)
    return handle_content_type(response)


@blueprint.route('/<short_name>/')
@login_required
def details(short_name):
    project, owner, ps = project_by_shortname(short_name)
    num_available_tasks = n_available_tasks(project.id, current_user.id)
    num_completed_tasks_by_user = n_completed_tasks_by_user(project.id, current_user.id)
    oldest_task = oldest_available_task(project.id, current_user.id)
    num_available_tasks_for_user = n_available_tasks_for_user(project, current_user.id)
    latest_submission_date = latest_submission_task_date(project.id)

    if project.needs_password():
        redirect_to_password = _check_if_redirect_to_password(project)
        if redirect_to_password:
            return redirect_to_password
    else:
        ensure_authorized_to('read', project)

    template = '/projects/project.html'
    pro = pro_features()

    title = project_title(project, None)
    project = add_custom_contrib_button_to(project, get_user_id_or_ip(), ps=ps)
    project_sanitized, owner_sanitized = sanitize_project_owner(project, owner,
                                                                current_user,
                                                                ps)
    template_args = {"project": project_sanitized,
                     "title": title,
                     "owner":  owner_sanitized,
                     "n_tasks": ps.n_tasks,
                     "n_task_runs": ps.n_task_runs,
                     "overall_progress": ps.overall_progress,
                     "last_activity": ps.last_activity,
                     "n_completed_tasks": ps.n_completed_tasks,
                     "n_volunteers": ps.n_volunteers,
                     "pro_features": pro,
                     "n_available_tasks": num_available_tasks,
                     "n_completed_tasks_by_user": num_completed_tasks_by_user,
                     "oldest_available_task": oldest_task,
                     "n_available_tasks_for_user": num_available_tasks_for_user,
                     "latest_submitted_task": latest_submission_date
                     }
    if current_app.config.get('CKAN_URL'):
        template_args['ckan_name'] = current_app.config.get('CKAN_NAME')
        template_args['ckan_url'] = current_app.config.get('CKAN_URL')
        template_args['ckan_pkg_name'] = short_name
    response = dict(template=template, **template_args)
    return handle_content_type(response)


@blueprint.route('/<short_name>/settings')
@login_required
def settings(short_name):
    project, owner, ps = project_by_shortname(short_name)

    title = project_title(project, "Settings")
    ensure_authorized_to('read', project)
    ensure_authorized_to('update', project)
    pro = pro_features()
    project = add_custom_contrib_button_to(project, get_user_id_or_ip(), ps=ps)
    owner_serialized = cached_users.get_user_summary(owner.name)
    response = dict(template='/projects/settings.html',
                    project=project,
                    owner=owner_serialized,
                    n_tasks=ps.n_tasks,
                    overall_progress=ps.overall_progress,
                    n_task_runs=ps.n_task_runs,
                    last_activity=ps.last_activity,
                    n_completed_tasks=ps.n_completed_tasks,
                    n_volunteers=ps.n_volunteers,
                    title=title,
                    pro_features=pro)
    return handle_content_type(response)


@blueprint.route('/<short_name>/tasks/import', methods=['GET', 'POST'])
@login_required
def import_task(short_name):
    project, owner, ps = project_by_shortname(short_name)

    ensure_authorized_to('read', project)
    ensure_authorized_to('update', project)

    title = project_title(project, "Import Tasks")
    loading_text = gettext("Importing tasks, this may take a while, wait...")
    pro = pro_features()
    dict_project = add_custom_contrib_button_to(project, get_user_id_or_ip(), ps=ps)
    project_sanitized, owner_sanitized = sanitize_project_owner(dict_project,
                                                                owner,
                                                                current_user,
                                                                ps)
    template_args = dict(title=title, loading_text=loading_text,
                         project=project_sanitized,
                         owner=owner_sanitized,
                         n_tasks=ps.n_tasks,
                         overall_progress=ps.overall_progress,
                         n_volunteers=ps.n_volunteers,
                         n_completed_tasks=ps.n_completed_tasks,
                         target='project.import_task',
                         pro_features=pro)

    importer_type = request.form.get('form_name') or request.args.get('type')
    all_importers = importer.get_all_importer_names()
    if importer_type is not None and importer_type not in all_importers:
        return abort(404)
    form = GenericBulkTaskImportForm()(importer_type, request.body)
    template_args['form'] = form

    if request.method == 'POST':
        if form.validate():  # pragma: no cover
            try:
                return _import_tasks(project, **form.get_import_data())
            except BulkImportException as e:
                flash(gettext(unicode(e)), 'error')
                current_app.logger.exception(u'project: {} {}'.format(project.short_name, e))
            except Exception as e:
                msg = 'Oops! Looks like there was an error!'
                flash(gettext(msg), 'error')
                current_app.logger.exception(u'project: {} {}'.format(project.short_name, e))
        template_args['template'] = '/projects/importers/%s.html' % importer_type
        return handle_content_type(template_args)

    if request.method == 'GET':
        template_tasks = current_app.config.get('TEMPLATE_TASKS')
        if importer_type is None:
            return redirect_content_type(url_for('.import_task',
                                                 short_name=short_name,
                                                 type=all_importers[0]))
            template_wrap = lambda i: "projects/tasks/gdocs-%s.html" % i
            task_tmpls = map(template_wrap, template_tasks)
            template_args['task_tmpls'] = task_tmpls
            importer_wrap = lambda i: "projects/tasks/%s.html" % i
            template_args['available_importers'] = map(importer_wrap, all_importers)
            template_args['template'] = '/projects/task_import_options.html'
            return handle_content_type(template_args)
        if importer_type == 'gdocs' and request.args.get('template'):  # pragma: no cover
            template = request.args.get('template')
            form.googledocs_url.data = template_tasks.get(template)
        template_args['template'] = '/projects/importers/%s.html' % importer_type
        return handle_content_type(template_args)


def _import_tasks(project, **form_data):
    number_of_tasks = importer.count_tasks_to_import(**form_data)
    if number_of_tasks <= MAX_NUM_SYNCHRONOUS_TASKS_IMPORT:
        report = importer.create_tasks(task_repo, project, **form_data)
        flash(report.message)
        if report.total > 0:
            cached_projects.delete_browse_tasks(project.id)
    else:
        importer_queue.enqueue(import_tasks, project.id, current_user.fullname, **form_data)
        flash(gettext("You're trying to import a large amount of tasks, so please be patient.\
            You will receive an email when the tasks are ready."))
    return redirect_content_type(url_for('.tasks',
                                         short_name=project.short_name))


@blueprint.route('/<short_name>/tasks/autoimporter', methods=['GET', 'POST'])
@login_required
@admin_required
def setup_autoimporter(short_name):
    pro = pro_features()
    if not pro['autoimporter_enabled']:
        raise abort(403)

    project, owner, ps = project_by_shortname(short_name)

    dict_project = add_custom_contrib_button_to(project, get_user_id_or_ip(), ps=ps)
    template_args = dict(project=dict_project,
                         owner=owner,
                         n_tasks=ps.n_tasks,
                         overall_progress=ps.overall_progress,
                         n_volunteers=ps.n_volunteers,
                         n_completed_tasks=ps.n_completed_tasks,
                         pro_features=pro,
                         target='project.setup_autoimporter')
    ensure_authorized_to('read', project)
    ensure_authorized_to('update', project)
    importer_type = request.form.get('form_name') or request.args.get('type')
    all_importers = importer.get_autoimporter_names()
    if importer_type is not None and importer_type not in all_importers:
        raise abort(404)
    form = GenericBulkTaskImportForm()(importer_type, request.form)
    template_args['form'] = form

    if project.has_autoimporter():
        current_autoimporter = project.get_autoimporter()
        importer_info = dict(**current_autoimporter)
        return render_template('/projects/task_autoimporter.html',
                                importer=importer_info, **template_args)

    if request.method == 'POST':
        if form.validate():  # pragma: no cover
            project.set_autoimporter(form.get_import_data())
            project_repo.save(project)
            auditlogger.log_event(project, current_user, 'create', 'autoimporter',
                                  'Nothing', json.dumps(project.get_autoimporter()))
            flash(gettext("Success! Tasks will be imported daily."))
            return redirect(url_for('.setup_autoimporter', short_name=project.short_name))

    if request.method == 'GET':
        if importer_type is None:
            wrap = lambda i: "projects/tasks/%s.html" % i
            template_args['available_importers'] = map(wrap, all_importers)
            return render_template('projects/task_autoimport_options.html',
                                   **template_args)
    return render_template('/projects/importers/%s.html' % importer_type,
                                **template_args)


@blueprint.route('/<short_name>/tasks/autoimporter/delete', methods=['POST'])
@login_required
@admin_required
def delete_autoimporter(short_name):
    pro = pro_features()
    if not pro['autoimporter_enabled']:
        raise abort(403)

    project = project_by_shortname(short_name)[0]

    ensure_authorized_to('read', project)
    ensure_authorized_to('update', project)
    if project.has_autoimporter():
        autoimporter = project.get_autoimporter()
        project.delete_autoimporter()
        project_repo.save(project)
        auditlogger.log_event(project, current_user, 'delete', 'autoimporter',
                              json.dumps(autoimporter), 'Nothing')
    return redirect(url_for('.tasks', short_name=project.short_name))


@blueprint.route('/<short_name>/password', methods=['GET', 'POST'])
def password_required(short_name):
    project, owner, ps = project_by_shortname(short_name)
    form = PasswordForm(request.form)
    if request.method == 'POST' and form.validate():
        password = request.form.get('password')
        cookie_exp = current_app.config.get('PASSWD_COOKIE_TIMEOUT')
        passwd_mngr = ProjectPasswdManager(CookieHandler(request, signer, cookie_exp))
        if passwd_mngr.validates(password, project):
            response = make_response(redirect(request.args.get('next')))
            return passwd_mngr.update_response(response, project, get_user_id_or_ip())
        flash(gettext('Sorry, incorrect password'))
    return render_template('projects/password.html',
                            project=project,
                            form=form,
                            short_name=short_name,
                            next=request.args.get('next'))


@blueprint.route('/<short_name>/task/<int:task_id>')
@login_required
def task_presenter(short_name, task_id):
    project, owner, ps = project_by_shortname(short_name)
    task = task_repo.get_task(id=task_id)
    if task is None:
        raise abort(404)
    if project.needs_password():
        redirect_to_password = _check_if_redirect_to_password(project)
        if redirect_to_password:
            return redirect_to_password
    else:
        ensure_authorized_to('read', project)

    if not sched.can_read_task(task, current_user) and not current_user.id in project.owners_ids:
        raise abort(403)

    if current_user.is_anonymous():
        if not project.allow_anonymous_contributors:
            msg = ("Oops! You have to sign in to participate in "
                   "<strong>%s</strong>"
                   "project" % project.name)
            flash(Markup(gettext(msg)), 'warning')
            return redirect(url_for('account.signin',
                                    next=url_for('.presenter',
                                    short_name=project.short_name)))
        else:
            msg_1 = gettext(
                "Ooops! You are an anonymous user and will not "
                "get any credit"
                " for your contributions.")
            msg_2 = gettext('Sign in now!')
            next_url = url_for('project.task_presenter',
                                short_name=short_name, task_id=task_id)
            url = url_for('account.signin', next=next_url)
            markup = Markup('{{}} <a href="{}">{{}}</a>'.format(url))
            flash(markup.format(msg_1, msg_2), "warning")

    title = project_title(project, "Contribute")
    project_sanitized, owner_sanitized = sanitize_project_owner(project, owner,
                                                                current_user,
                                                                ps)
    template_args = {"project": project_sanitized, "title": title, "owner": owner_sanitized}

    def respond(tmpl):
        response = dict(template = tmpl, **template_args)
        return handle_content_type(response)

    if not (task.project_id == project.id):
        return respond('/projects/task/wrong.html')

    guard = ContributionsGuard(sentinel.master,
                               timeout=project.info.get('timeout'))
    guard.stamp(task, get_user_id_or_ip())

    if not guard.check_task_presented_timestamp(task, get_user_id_or_ip()):
        guard.stamp_presented_time(task, get_user_id_or_ip())

    if has_no_presenter(project):
        flash(gettext("Sorry, but this project is still a draft and does "
                      "not have a task presenter."), "error")
    return respond('/projects/presenter.html')


@blueprint.route('/<short_name>/presenter')
@blueprint.route('/<short_name>/newtask')
@login_required
def presenter(short_name):

    def invite_new_volunteers(project, ps):
        user_id = None if current_user.is_anonymous() else current_user.id
        user_ip = request.remote_addr if current_user.is_anonymous() else None
        task = sched.new_task(project.id, project.info.get('sched'), user_id, user_ip, 0)
        return task == [] and ps.overall_progress < 100.0

    def respond(tmpl):
        if (current_user.is_anonymous()):
            msg_1 = gettext(msg)
            flash(msg_1, "warning")
        resp = make_response(render_template(tmpl, **template_args))
        return resp

    project, owner, ps = project_by_shortname(short_name)

    if project.needs_password():
        redirect_to_password = _check_if_redirect_to_password(project)
        if redirect_to_password:
            return redirect_to_password
    else:
        ensure_authorized_to('read', project)

    title = project_title(project, "Contribute")
    template_args = {"project": project, "title": title, "owner": owner,
                     "invite_new_volunteers": invite_new_volunteers(project, ps)}

    if not project.allow_anonymous_contributors and current_user.is_anonymous():
        msg = "Oops! You have to sign in to participate in <strong>%s</strong> \
               project" % project.name
        flash(Markup(gettext(msg)), 'warning')
        return redirect(url_for('account.signin',
                        next=url_for('.presenter',
                                     short_name=project.short_name)))

    msg = "Ooops! You are an anonymous user and will not \
           get any credit for your contributions. Sign in \
           now!"

    if project.info.get("tutorial") and \
            request.cookies.get(project.short_name + "tutorial") is None:
        resp = respond('/projects/tutorial.html')
        resp.set_cookie(project.short_name + 'tutorial', 'seen')
        return resp
    else:
        if has_no_presenter(project):
            flash(gettext("Sorry, but this project is still a draft and does "
                          "not have a task presenter."), "error")
        return respond('/projects/presenter.html')


@blueprint.route('/<short_name>/tutorial')
def tutorial(short_name):
    project, owner, ps = project_by_shortname(short_name)
    title = project_title(project, "Tutorial")

    if project.needs_password():
        redirect_to_password = _check_if_redirect_to_password(project)
        if redirect_to_password:
            return redirect_to_password
    else:
        ensure_authorized_to('read', project)

    project_sanitized, owner_sanitized = sanitize_project_owner(project, owner,
                                                                current_user,
                                                                ps)

    response = dict(template='/projects/tutorial.html', title=title,
                    project=project_sanitized, owner=owner_sanitized)

    return handle_content_type(response)


@blueprint.route('/<short_name>/<int:task_id>/results.json')
@login_required
def export(short_name, task_id):
    """Return a file with all the TaskRuns for a given Task"""
    # Check if the project exists and current_user has valid access to it
    project, owner, ps = allow_deny_project_info(short_name)

    if project.needs_password():
        redirect_to_password = _check_if_redirect_to_password(project)
        if redirect_to_password:
            return redirect_to_password
    else:
        ensure_authorized_to('read', project)

    # Check if the task belongs to the project and exists
    task = task_repo.get_task_by(project_id=project.id, id=task_id)
    if task:
        taskruns = task_repo.filter_task_runs_by(task_id=task_id, project_id=project.id)
        results = [tr.dictize() for tr in taskruns]
        return Response(json.dumps(results), mimetype='application/json')
    else:
        return abort(404)


@blueprint.route('/<short_name>/<int:task_id>/result_status')
@login_required
def export_statuses(short_name, task_id):
    """Return a file with all TaskRun statuses for a given Task"""
    project, owner, ps = allow_deny_project_info(short_name)

    if project.needs_password():
        redirect_to_password = _check_if_redirect_to_password(project)
        if redirect_to_password:
            return redirect_to_password
    else:
        ensure_authorized_to('read', project)

    task = task_repo.get_task(task_id)

    if not task:
        return abort(404)

    locks = _get_locks(project.id, task_id)
    users_completed = [tr.user_id for tr in task.task_runs]
    users = user_repo.get_users(
            set(users_completed + locks.keys()))
    user_details = [dict(user_id=user.id,
                         lock_ttl=locks.get(user.id),
                         user_email=user.email_addr)
                    for user in users]

    for user_detail in user_details:
        if user_detail['user_id'] in users_completed:
            user_detail['status'] = 'Completed'
        elif user_detail['lock_ttl']:
            user_detail['status'] = 'Locked'

    tr_statuses = dict(redundancy=task.n_answers,
                       user_details=user_details)

    return jsonify(tr_statuses)


def _get_locks(project_id, task_id):
    _sched, timeout = sched.get_project_scheduler_and_timeout(
            project_id)
    locks = sched.get_locks(project_id, task_id, timeout)
    now = time.time()
    lock_ttls = {int(k): float(v) - now
                 for k, v in locks.iteritems()}
    return lock_ttls


@blueprint.route('/<short_name>/tasks/')
@login_required
def tasks(short_name):
    project, owner, ps = project_by_shortname(short_name)
    title = project_title(project, "Tasks")

    if project.needs_password():
        redirect_to_password = _check_if_redirect_to_password(project)
        if redirect_to_password:
            return redirect_to_password
    else:
        ensure_authorized_to('read', project)

    pro = pro_features()
    project = add_custom_contrib_button_to(project, get_user_id_or_ip())
    feature_handler = ProFeatureHandler(current_app.config.get('PRO_FEATURES'))
    autoimporter_enabled = feature_handler.autoimporter_enabled_for(current_user)

    project_sanitized, owner_sanitized = sanitize_project_owner(project,
                                                                owner,
                                                                current_user,
                                                                ps)

    response = dict(template='/projects/tasks.html',
                    title=title,
                    project=project_sanitized,
                    owner=owner_sanitized,
                    autoimporter_enabled=autoimporter_enabled,
                    n_tasks=ps.n_tasks,
                    n_task_runs=ps.n_task_runs,
                    overall_progress=ps.overall_progress,
                    last_activity=ps.last_activity,
                    n_completed_tasks=ps.n_completed_tasks,
                    n_volunteers=ps.n_volunteers,
                    pro_features=pro)

    return handle_content_type(response)


@blueprint.route('/<short_name>/tasks/browse')
@blueprint.route('/<short_name>/tasks/browse/<int:page>')
@blueprint.route('/<short_name>/tasks/browse/<int:page>/<int:records_per_page>')
@login_required
def tasks_browse(short_name, page=1, records_per_page=10):
    project, owner, ps = allow_deny_project_info(short_name)

    title = project_title(project, "Tasks")
    pro = pro_features()
    allowed_records_per_page = [10, 20, 30, 50, 70, 100]

    try:
        columns = get_searchable_columns(project.id)
    except:
        current_app.logger.exception('Error getting columns')
        columns = []

    try:
        args = parse_tasks_browse_args(request.args)
    except (ValueError, TypeError) as err:
        current_app.logger.exception(err)
        flash(gettext('Invalid filtering criteria'), 'error')
        abort(404)

    def respond():
        if records_per_page in allowed_records_per_page:
            per_page = records_per_page
        else:
            per_page = 10
        offset = (page - 1) * per_page
        args["records_per_page"] = per_page
        args["offset"] = offset
        start_time = time.time()
        (count, page_tasks) = cached_projects.browse_tasks(project.get('id'), args)
        current_app.logger.debug("Browse Tasks data loading took %s seconds"
                                 % (time.time()-start_time))
        first_task_id = cached_projects.first_task_id(project.get('id'))

        pagination = Pagination(page, per_page, count)

        project_sanitized, owner_sanitized = sanitize_project_owner(project,
                                                                    owner,
                                                                    current_user,
                                                                    ps)
        args["changed"] = False
        if args.get("pcomplete_from"):
            args["pcomplete_from"] = args["pcomplete_from"] * 100
        if args.get("pcomplete_to"):
            args["pcomplete_to"] = args["pcomplete_to"] * 100
        args["order_by"] = args.pop("order_by_dict", dict())
        args.pop("records_per_page", None)
        args.pop("offset", None)

        for task in page_tasks:
            task_info = task_repo.get_task(task['id']).info
            for col in columns:
                task[col] = task_info.get(col, '')
        info_columns = [col for col in columns if col in args['display_columns']]

        data = dict(template='/projects/tasks_browse.html',
                    project=project_sanitized,
                    owner=owner_sanitized,
                    tasks=page_tasks,
                    title=title,
                    pagination=pagination,
                    n_tasks=ps.n_tasks,
                    overall_progress=ps.overall_progress,
                    n_volunteers=ps.n_volunteers,
                    n_completed_tasks=ps.n_completed_tasks,
                    pro_features=pro,
                    allowed_records_per_page=allowed_records_per_page,
                    records_per_page=records_per_page,
                    filter_data=args,
                    first_task_id=first_task_id,
                    info_columns=info_columns,
                    filter_columns=columns)

        return handle_content_type(data)

    def respond_export(download_type, args):
        try:
            download_specs = download_type.split('-')
            download_obj = download_specs[0]
            download_format = download_specs[1]

            try:
                metadata = bool(download_specs[2])
            except:
                metadata = False

            assert download_obj in ('task', 'task_run')
            assert download_format in ('csv', 'json')
        except:
            current_app.logger.exception('Invalid download type {0} for project {1}.'
                                         .format(download_type, project.short_name))
            flash(gettext('Invalid download type. Please try again.'), 'error')

        try:
            export_queue.enqueue(export_tasks,
                                 current_user.email_addr,
                                 short_name,
                                 ty=download_obj,
                                 expanded=metadata,
                                 filetype=download_format,
                                 **args)
            flash(gettext('You will be emailed when your export has been completed.'),
                  'success')
        except:
            current_app.logger.exception(
                    '{0} Export Failed - Project: {1}, Type: {2}'
                    .format(download_type.upper(), project.short_name, ty))
            flash(gettext('There was an error while exporting your data.'),
                  'error')

        return respond()

    if project.needs_password():
        redirect_to_password = _check_if_redirect_to_password(project)
        if redirect_to_password:
            return redirect_to_password
    else:
        ensure_authorized_to('read', project)

    project = add_custom_contrib_button_to(project, get_user_id_or_ip())

    download_type = request.args.get('download_type')

    if download_type:
        return respond_export(download_type, args)
    else:
        return respond()


@crossdomain(origin='*', headers=cors_headers)
@blueprint.route('/<short_name>/tasks/priorityupdate', methods=['POST'])
@login_required
@admin_or_subadmin_required
def bulk_priority_update(short_name):
    try:
        project, owner, ps = project_by_shortname(short_name)
        ensure_authorized_to('read', project)
        ensure_authorized_to('update', project)
        req_data = request.json
        priority_0 = req_data.get('priority_0', 0)
        task_ids = req_data.get('taskIds')
        if task_ids:
            current_app.logger.info(task_ids)
            for task_id in task_ids:
                if task_id != '':
                    t = task_repo.get_task_by(project_id=project.id,
                                              id=int(task_id))
                    if t and t.priority_0 != priority_0:
                        t.priority_0 = priority_0
                        task_repo.update(t)
            new_value = json.dumps({
                'task_ids': task_ids,
                'priority_0': priority_0
            })
        else:
            args = parse_tasks_browse_args(request.json.get('filters'))
            task_repo.update_priority(project.id, priority_0, args)
            new_value = json.dumps({
                'filters': args,
                'priority_0': priority_0
            })

        auditlogger.log_event(project, current_user, 'bulk update priority',
                              'task.priority_0', 'N/A', new_value)
        return Response('{}', 200, mimetype='application/json')
    except Exception as e:
        return ErrorStatus().format_exception(e, 'priorityupdate', 'POST')


@crossdomain(origin='*', headers=cors_headers)
@blueprint.route('/<short_name>/tasks/redundancyupdate', methods=['POST'])
@login_required
@admin_or_subadmin_required
def bulk_redundancy_update(short_name):
    try:
        project, owner, ps = project_by_shortname(short_name)
        ensure_authorized_to('read', project)
        ensure_authorized_to('update', project)
        req_data = request.json
        n_answers = req_data.get('n_answers', 1)
        task_ids = req_data.get('taskIds')
        if task_ids:
            _update_task_redundancy(project.id, task_ids, n_answers)
            new_value = json.dumps({
                'task_ids': task_ids,
                'n_answers': n_answers
            })
        else:
            args = parse_tasks_browse_args(request.json.get('filters'))
            task_repo.update_tasks_redundancy(project, n_answers, args)
            new_value = json.dumps({
                'filters': args,
                'n_answers': n_answers
            })

        auditlogger.log_event(project, current_user, 'bulk update redundancy',
                              'task.n_answers', 'N/A', new_value)
        return Response('{}', 200, mimetype='application/json')
    except Exception as e:
        return ErrorStatus().format_exception(e, 'redundancyupdate', 'POST')


def _update_task_redundancy(project_id, task_ids, n_answers):
    """
    Update the redundancy for a list of tasks in a given project. Mark tasks
    exported as False for tasks with curr redundancy < new redundancy
    and task was already exported
    """
    for task_id in task_ids:
        if task_id:
            t = task_repo.get_task_by(project_id=project_id,
                                      id=int(task_id))
            if t and t.n_answers != n_answers:
                if len(t.task_runs) < n_answers and t.state == 'completed':
                    t.exported = False
                t.n_answers = n_answers
                t.state = 'ongoing'
                if len(t.task_runs) >= n_answers:
                    t.state = 'completed'
                task_repo.update(t)


@crossdomain(origin='*', headers=cors_headers)
@blueprint.route('/<short_name>/tasks/deleteselected', methods=['POST'])
@login_required
@admin_or_subadmin_required
def delete_selected_tasks(short_name):
    try:
        project, owner, ps = project_by_shortname(short_name)
        ensure_authorized_to('read', project)
        ensure_authorized_to('update', project)
        req_data = request.json
        task_ids = req_data.get('taskIds')
        if task_ids:
            for task_id in task_ids:
                task_repo.delete_task_by_id(project.id, task_id)
            new_value = json.dumps({
                'task_ids': task_ids,
            })
            async = False
        else:
            args = parse_tasks_browse_args(request.json.get('filters'))
            count = cached_projects.task_count(project.id, args)
            async = count > MAX_NUM_SYNCHRONOUS_TASKS_DELETE
            if async:
                owners = user_repo.get_users(project.owners_ids)
                data = {
                    'project_id': project.id, 'project_name': project.name,
                    'curr_user': current_user.email_addr, 'force_reset': True,
                    'coowners': owners, 'filters': args,
                    'current_user_fullname': current_user.fullname}
                task_queue.enqueue(delete_bulk_tasks, data)
            else:
                task_repo.delete_valid_from_project(project, True, args)

            new_value = json.dumps({
                'filters': args
            })

        auditlogger.log_event(project, current_user, 'delete tasks',
                              'task', 'N/A', new_value)
        return Response(json.dumps(dict(enqueued=async)), 200,
                        mimetype='application/json')
    except Exception as e:
        return ErrorStatus().format_exception(e, 'deleteselected', 'POST')


@blueprint.route('/<short_name>/tasks/delete', methods=['GET', 'POST'])
@login_required
def delete_tasks(short_name):
    """Delete ALL the tasks for a given project"""
    project, owner, ps = project_by_shortname(short_name)
    ensure_authorized_to('read', project)
    ensure_authorized_to('update', project)
    pro = pro_features()
    if request.method == 'GET':
        title = project_title(project, "Delete")
        n_volunteers = cached_projects.n_volunteers(project.id)
        n_completed_tasks = cached_projects.n_completed_tasks(project.id)
        project = add_custom_contrib_button_to(project, get_user_id_or_ip())
        project_sanitized, owner_sanitized = sanitize_project_owner(project,
                                                                    owner,
                                                                    current_user,
                                                                    ps)
        response = dict(template='projects/tasks/delete.html',
                        project=project_sanitized,
                        owner=owner_sanitized,
                        n_tasks=ps.n_tasks,
                        n_task_runs=ps.n_task_runs,
                        n_volunteers=ps.n_volunteers,
                        n_completed_tasks=ps.n_completed_tasks,
                        overall_progress=ps.overall_progress,
                        last_activity=ps.last_activity,
                        title=title,
                        pro_features=pro,
                        csrf=generate_csrf())
        return handle_content_type(response)
    else:
        force_reset = request.form.get("force_reset") == 'true'
        if ps.n_tasks <= MAX_NUM_SYNCHRONOUS_TASKS_DELETE:
            task_repo.delete_valid_from_project(project, force_reset=force_reset)
            if not force_reset:
                msg = gettext("Tasks and taskruns with no associated results have been deleted")
            else:
                msg = gettext("All tasks, taskruns and results associated with this project have been deleted")

            flash(msg, 'success')
        else:
            owners = user_repo.get_users(project.owners_ids)
            data = {'project_id': project.id, 'project_name': project.name,
                    'curr_user': current_user.email_addr, 'force_reset': force_reset,
                    'coowners': owners, 'current_user_fullname': current_user.fullname}
            task_queue.enqueue(delete_bulk_tasks, data)
            flash(gettext("You're trying to delete a large amount of tasks, so please be patient.\
                    You will receive an email when the tasks deletion is complete."))
        return redirect_content_type(url_for('.tasks', short_name=project.short_name))


@blueprint.route('/<short_name>/tasks/export')
@login_required
def export_to(short_name):
    """Export Tasks and TaskRuns in the given format"""
    project, owner, ps = allow_deny_project_info(short_name)
    supported_tables = ['task', 'task_run', 'result']

    title = project_title(project, gettext("Export"))
    loading_text = gettext("Exporting data..., this may take a while")
    pro = pro_features()

    if project.needs_password():
        redirect_to_password = _check_if_redirect_to_password(project)
        if redirect_to_password:
            return redirect_to_password

    def respond():
        return render_template('/projects/export.html',
                               title=title,
                               loading_text=loading_text,
                               ckan_name=current_app.config.get('CKAN_NAME'),
                               project=project,
                               owner=owner,
                               n_tasks=ps.n_tasks,
                               n_task_runs=ps.n_task_runs,
                               n_volunteers=ps.n_volunteers,
                               n_completed_tasks=ps.n_completed_tasks,
                               overall_progress=ps.overall_progress,
                               pro_features=pro)
    def respond_json(ty, expanded):
        if ty not in ('task', 'task_run'):
            return abort(404)

        try:
            export_queue.enqueue(export_tasks,
                                 current_user.email_addr,
                                 short_name,
                                 ty,
                                 expanded,
                                 'json')
            flash(gettext('You will be emailed when your export has been completed.'),
                  'success')
        except Exception as e:
            current_app.logger.exception(
                    'JSON Export Failed - Project: {0}, Type: {1} - Error: {2}'
                    .format(project.short_name, ty, e))
            flash(gettext('There was an error while exporting your data.'),
                  'error')

        return respond()

    def respond_csv(ty, expanded):
        if ty not in ('task', 'task_run'):
            return abort(404)

        try:
            export_queue.enqueue(export_tasks,
                                 current_user.email_addr,
                                 short_name,
                                 ty,
                                 expanded,
                                 'csv')
            flash(gettext('You will be emailed when your export has been completed.'),
                  'success')
        except Exception as e:
            current_app.logger.exception(
                    'CSV Export Failed - Project: {0}, Type: {1} - Error: {2}'
                    .format(project.short_name, ty, e))
            flash(gettext('There was an error while exporting your data.'),
                  'error')

        return respond()

    def create_ckan_datastore(ckan, table, package_id, records):
        new_resource = ckan.resource_create(name=table,
                                            package_id=package_id)
        ckan.datastore_create(name=table,
                              resource_id=new_resource['result']['id'])
        ckan.datastore_upsert(name=table,
                              records=records,
                              resource_id=new_resource['result']['id'])

    def respond_ckan(ty, expanded):
        # First check if there is a package (dataset) in CKAN
        msg_1 = gettext("Data exported to ")
        msg = msg_1 + "%s ..." % current_app.config['CKAN_URL']
        ckan = Ckan(url=current_app.config['CKAN_URL'],
                    api_key=current_user.ckan_api)
        project_url = url_for('.details', short_name=project.short_name, _external=True)

        try:
            package, e = ckan.package_exists(name=project.short_name)
            records = task_json_exporter.gen_json(ty, project.id, expanded)
            if e:
                raise e
            if package:
                # Update the package
                owner = user_repo.get(project.owner_id)
                package = ckan.package_update(project=project, user=owner,
                                              url=project_url,
                                              resources=package['resources'])

                ckan.package = package
                resource_found = False
                for r in package['resources']:
                    if r['name'] == ty:
                        ckan.datastore_delete(name=ty, resource_id=r['id'])
                        ckan.datastore_create(name=ty, resource_id=r['id'])
                        ckan.datastore_upsert(name=ty,
                                              records=records,
                                              resource_id=r['id'])
                        resource_found = True
                        break
                if not resource_found:
                    create_ckan_datastore(ckan, ty, package['id'], records)
            else:
                owner = user_repo.get(project.owner_id)
                package = ckan.package_create(project=project, user=owner,
                                              url=project_url)
                create_ckan_datastore(ckan, ty, package['id'], records)
            flash(msg, 'success')
            return respond()
        except requests.exceptions.ConnectionError:
            msg = "CKAN server seems to be down, try again layer or contact the CKAN admins"
            current_app.logger.error(msg)
            flash(msg, 'danger')
        except Exception as inst:
            print inst
            if len(inst.args) == 3:
                t, msg, status_code = inst.args
                msg = ("Error: %s with status code: %s" % (t, status_code))
            else:  # pragma: no cover
                msg = ("Error: %s" % inst.args[0])
            current_app.logger.error(msg)
            flash(msg, 'danger')
        finally:
            return respond()

    export_formats = ["json", "csv"]
    if current_user.is_authenticated():
        if current_user.ckan_api:
            export_formats.append('ckan')

    ty = request.args.get('type')
    fmt = request.args.get('format')
    expanded = False
    if request.args.get('expanded') == 'True':
        expanded = True

    if not (fmt and ty):
        if len(request.args) >= 1:
            abort(404)
        project = add_custom_contrib_button_to(project, get_user_id_or_ip(), ps=ps)
        return respond()

    if fmt not in export_formats:
        abort(415)

    if ty == 'task':
        task = task_repo.get_task_by(project_id=project.id)
        if task:
            ensure_authorized_to('read', task)
    if ty == 'task_run':
        task_run = task_repo.get_task_run_by(project_id=project.id)
        if task_run:
            ensure_authorized_to('read', task_run)

    return {"json": respond_json,
            "csv": respond_csv,
            'ckan': respond_ckan}[fmt](ty, expanded)


@blueprint.route('/export')
@login_required
@admin_required
def export_projects():
    """Export projects list, only for admins."""

    def respond_csv():
        import datetime
        info = AttrDict(id=0, short_name=datetime.datetime.now().isoformat(),
                        owner_id=current_user.id,
                        base_url=request.url_root+'project/')

        return project_csv_exporter.response_zip(info, 'projects')

    export_formats = ["csv"]

    fmt = request.args.get('format')
    if not fmt:
        return redirect(url_for('.index'))
    if fmt not in export_formats:
        abort(415)
    return {"csv": respond_csv}[fmt]()


@blueprint.route('/<short_name>/stats')
@login_required
def show_stats(short_name):
    """Returns Project Stats"""
    project, owner, ps = project_by_shortname(short_name)
    n_completed_tasks = cached_projects.n_completed_tasks(project.id)
    n_pending_tasks = ps.n_tasks-n_completed_tasks
    title = project_title(project, "Statistics")
    pro = pro_features(owner)

    if project.needs_password():
        redirect_to_password = _check_if_redirect_to_password(project)
        if redirect_to_password:
            return redirect_to_password
    else:
        ensure_authorized_to('read', project)

    project_sanitized, owner_sanitized = sanitize_project_owner(project,
                                                                owner,
                                                                current_user,
                                                                ps)

    if not ((ps.n_tasks > 0) and (ps.n_task_runs > 0)):
        project = add_custom_contrib_button_to(project, get_user_id_or_ip(),
                                               ps=ps)
        response = dict(template='/projects/non_stats.html',
                        title=title,
                        project=project_sanitized,
                        owner=owner_sanitized,
                        n_tasks=ps.n_tasks,
                        overall_progress=ps.overall_progress,
                        n_volunteers=ps.n_volunteers,
                        n_completed_tasks=ps.n_completed_tasks,
                        pro_features=pro)
        return handle_content_type(response)

    dates_stats = ps.info['dates_stats']
    hours_stats = ps.info['hours_stats']
    users_stats = ps.info['users_stats']

    total_contribs = (users_stats['n_anon'] + users_stats['n_auth'])
    if total_contribs > 0:
        anon_pct_taskruns = int((users_stats['n_anon'] * 100) / total_contribs)
        auth_pct_taskruns = 100 - anon_pct_taskruns
    else:
        anon_pct_taskruns = 0
        auth_pct_taskruns = 0

    userStats = dict(
        geo=current_app.config['GEO'],
        anonymous=dict(
            users=users_stats['n_anon'],
            taskruns=users_stats['n_anon'],
            pct_taskruns=anon_pct_taskruns,
            top5=users_stats['anon']['top5']),
        authenticated=dict(
            users=users_stats['n_auth'],
            taskruns=users_stats['n_auth'],
            pct_taskruns=auth_pct_taskruns,
            top10=users_stats['auth']['top10']))

    projectStats = dict(
        userStats=users_stats['users'],
        userAnonStats=users_stats['anon'],
        userAuthStats=users_stats['auth'],
        dayStats=dates_stats,
        n_completed_tasks=n_completed_tasks,
        n_pending_tasks=n_pending_tasks,
        hourStats=hours_stats)

    project_dict = add_custom_contrib_button_to(project, get_user_id_or_ip(),
                                                ps=ps)
    formatted_contrib_time = round(ps.average_time/60, 2)

    project_sanitized, owner_sanitized = sanitize_project_owner(project, owner,
                                                                current_user,
                                                                ps)

    # Handle JSON project stats depending of output
    # (needs to be escaped for HTML)
    if request.headers.get('Content-Type') == 'application/json':
        handle_projectStats = projectStats
    else:   # HTML
        handle_projectStats = json.dumps(projectStats)

    response = dict(template='/projects/stats.html',
                    title=title,
                    projectStats=handle_projectStats,
                    userStats=userStats,
                    project=project_sanitized,
                    owner=owner_sanitized,
                    n_tasks=ps.n_tasks,
                    overall_progress=ps.overall_progress,
                    n_volunteers=ps.n_volunteers,
                    n_completed_tasks=ps.n_completed_tasks,
                    avg_contrib_time=formatted_contrib_time,
                    pro_features=pro)

    return handle_content_type(response)


@blueprint.route('/<short_name>/tasks/settings')
@login_required
def task_settings(short_name):
    """Settings page for tasks of the project"""
    project, owner, ps = project_by_shortname(short_name)

    ensure_authorized_to('read', project)
    ensure_authorized_to('update', project)
    pro = pro_features()
    project = add_custom_contrib_button_to(project, get_user_id_or_ip(), ps=ps)
    return render_template('projects/task_settings.html',
                           project=project,
                           owner=owner,
                           n_tasks=ps.n_tasks,
                           overall_progress=ps.overall_progress,
                           n_volunteers=ps.n_volunteers,
                           n_completed_tasks=ps.n_completed_tasks,
                           pro_features=pro)


@blueprint.route('/<short_name>/tasks/redundancy', methods=['GET', 'POST'])
@login_required
def task_n_answers(short_name):
    project, owner, ps = project_by_shortname(short_name)

    title = project_title(project, gettext('Redundancy'))
    form = TaskRedundancyForm(request.body)
    ensure_authorized_to('read', project)
    ensure_authorized_to('update', project)
    pro = pro_features()
    project_sanitized, owner_sanitized = sanitize_project_owner(project,
                                                                owner,
                                                                current_user,
                                                                ps)
    if request.method == 'GET':
        response = dict(template='/projects/task_n_answers.html',
                        title=title,
                        form=form,
                        project=project_sanitized,
                        owner=owner_sanitized,
                        pro_features=pro)
        return handle_content_type(response)
    elif request.method == 'POST' and form.validate():
        task_repo.update_tasks_redundancy(project, form.n_answers.data)
        # Log it
        auditlogger.log_event(project, current_user, 'update', 'task.n_answers',
                              'N/A', form.n_answers.data)
        msg = gettext('Redundancy of Tasks updated!')
        flash(msg, 'success')
        return redirect_content_type(url_for('.tasks', short_name=project.short_name))
    else:
        flash(gettext('Please correct the errors'), 'error')
        response = dict(template='/projects/task_n_answers.html',
                        title=title,
                        form=form,
                        project=project_sanitized,
                        owner=owner_sanitized,
                        pro_features=pro)
        return handle_content_type(response)


@blueprint.route('/<short_name>/tasks/scheduler', methods=['GET', 'POST'])
@login_required
def task_scheduler(short_name):
    project, owner, ps = project_by_shortname(short_name)

    title = project_title(project, gettext('Task Scheduler'))
    form = TaskSchedulerForm(request.body)
    pro = pro_features()


    def respond():
        project_sanitized, owner_sanitized = sanitize_project_owner(project,
                                                                    owner,
                                                                    current_user,
                                                                    ps)
        response = dict(template='/projects/task_scheduler.html',
                        title=title,
                        form=form,
                        project=project_sanitized,
                        owner=owner_sanitized,
                        pro_features=pro)
        return handle_content_type(response)

    ensure_authorized_to('read', project)
    ensure_authorized_to('update', project)

    if request.method == 'GET':
        if project.info.get('sched'):
            for s in form.sched.choices:
                if project.info['sched'] == s[0]:
                    form.sched.data = s[0]
                    break
        return respond()

    if request.method == 'POST' and form.validate():
        project = project_repo.get_by_shortname(short_name=project.short_name)
        if project.info.get('sched'):
            old_sched = project.info['sched']
        else:
            old_sched = 'default'
        if form.sched.data:
            project.info['sched'] = form.sched.data
        project_repo.save(project)
        # Log it
        if old_sched != project.info['sched']:
            auditlogger.log_event(project, current_user, 'update', 'sched',
                                  old_sched, project.info['sched'])
        msg = gettext("Project Task Scheduler updated!")
        flash(msg, 'success')

        return redirect_content_type(url_for('.tasks', short_name=project.short_name))
    else:  # pragma: no cover
        flash(gettext('Please correct the errors'), 'error')
        return respond()


@blueprint.route('/<short_name>/tasks/priority', methods=['GET', 'POST'])
@login_required
def task_priority(short_name):
    project, owner, ps = project_by_shortname(short_name)

    title = project_title(project, gettext('Task Priority'))
    form = TaskPriorityForm(request.body)
    pro = pro_features()

    def respond():
        project_sanitized, owner_sanitized = sanitize_project_owner(project,
                                                                    owner,
                                                                    current_user,
                                                                    ps)
        response = dict(template='/projects/task_priority.html',
                        title=title,
                        form=form,
                        project=project_sanitized,
                        owner=owner_sanitized,
                        pro_features=pro)
        return handle_content_type(response)
    ensure_authorized_to('read', project)
    ensure_authorized_to('update', project)

    if request.method == 'GET':
        return respond()
    if request.method == 'POST' and form.validate():
        for task_id in form.task_ids.data.split(","):
            if task_id != '':
                t = task_repo.get_task_by(project_id=project.id, id=int(task_id))
                if t:
                    old_priority = t.priority_0
                    t.priority_0 = form.priority_0.data
                    task_repo.update(t)

                    if old_priority != t.priority_0:
                        old_value = json.dumps({'task_id': t.id,
                                                'task_priority_0': old_priority})
                        new_value = json.dumps({'task_id': t.id,
                                                'task_priority_0': t.priority_0})
                        auditlogger.log_event(project, current_user, 'update',
                                              'task.priority_0',
                                              old_value, new_value)
                else:  # pragma: no cover
                    flash(gettext(("Ooops, Task.id=%s does not belong to the project" % task_id)), 'danger')
        flash(gettext("Task priority has been changed"), 'success')
        return respond()
    else:
        flash(gettext('Please correct the errors'), 'error')
        return respond()


@blueprint.route('/<short_name>/tasks/timeout', methods=['GET', 'POST'])
@login_required
def task_timeout(short_name):
    project, owner, ps = project_by_shortname(short_name)
    title = project_title(project, gettext('Timeout'))
    form = TaskTimeoutForm()
    ensure_authorized_to('read', project)
    ensure_authorized_to('update', project)
    pro = pro_features()
    if request.method == 'GET':
        timeout = project.info.get('timeout')
        if timeout is not None:
            form.timeout.data = timeout / 60
        return render_template('/projects/task_timeout.html',
                               title=title,
                               form=form,
                               project=project,
                               owner=owner,
                               pro_features=pro)
    elif request.method == 'POST' and form.validate():
        project = project_repo.get_by_shortname(short_name=project.short_name)
        if project.info.get('timeout'):
            old_timeout = project.info['timeout']
        else:
            old_timeout = DEFAULT_TASK_TIMEOUT
        if form.timeout.data:
            project.info['timeout'] = form.timeout.data * 60
        project_repo.save(project)
        # Log it
        if old_timeout != project.info.get('timeout'):
            auditlogger.log_event(project, current_user, 'update', 'timeout',
                                  old_timeout, project.info['timeout'])
        msg = gettext("Project Task Timeout updated!")
        flash(msg, 'success')

        return redirect(url_for('.tasks', short_name=project.short_name))
    else:
        flash(gettext('Please correct the errors'), 'error')
        return render_template('/projects/task_timeout.html',
                               title=title,
                               form=form,
                               project=project,
                               owner=owner,
                               pro_features=pro)


@blueprint.route('/<short_name>/blog')
def show_blogposts(short_name):
    project, owner, ps = project_by_shortname(short_name)

    if current_user.is_authenticated() and current_user.id == owner.id:
        blogposts = blog_repo.filter_by(project_id=project.id)
    else:
        blogposts = blog_repo.filter_by(project_id=project.id,
                                        published=True)
    if project.needs_password():
        redirect_to_password = _check_if_redirect_to_password(project)
        if redirect_to_password:
            return redirect_to_password
    else:
        ensure_authorized_to('read', Blogpost, project_id=project.id)
    pro = pro_features()
    project = add_custom_contrib_button_to(project, get_user_id_or_ip(), ps=ps)

    project_sanitized, owner_sanitized = sanitize_project_owner(project,
                                                                owner,
                                                                current_user,
                                                                ps)

    response = dict(template='projects/blog.html',
                    project=project_sanitized,
                    owner=owner_sanitized,
                    blogposts=blogposts,
                    overall_progress=ps.overall_progress,
                    n_tasks=ps.n_tasks,
                    n_task_runs=ps.n_task_runs,
                    n_completed_tasks=ps.n_completed_tasks,
                    n_volunteers=ps.n_volunteers,
                    pro_features=pro)
    return handle_content_type(response)


@blueprint.route('/<short_name>/<int:id>')
def show_blogpost(short_name, id):
    project, owner, ps = project_by_shortname(short_name)

    blogpost = blog_repo.get_by(id=id, project_id=project.id)
    if blogpost is None:
        raise abort(404)
    if current_user.is_anonymous() and blogpost.published is False:
        raise abort(404)
    if (blogpost.published is False and
            current_user.is_authenticated() and
            current_user.id != blogpost.user_id):
        raise abort(404)
    if project.needs_password():
        redirect_to_password = _check_if_redirect_to_password(project)
        if redirect_to_password:
            return redirect_to_password
    else:
        ensure_authorized_to('read', blogpost)
    pro = pro_features()
    project = add_custom_contrib_button_to(project, get_user_id_or_ip(), ps=ps)
    return render_template('projects/blog_post.html',
                           project=project,
                           owner=owner,
                           blogpost=blogpost,
                           overall_progress=ps.overall_progress,
                           n_tasks=ps.n_tasks,
                           n_task_runs=ps.n_task_runs,
                           n_completed_tasks=ps.n_completed_tasks,
                           n_volunteers=ps.n_volunteers,
                           pro_features=pro)


@blueprint.route('/<short_name>/new-blogpost', methods=['GET', 'POST'])
@login_required
def new_blogpost(short_name):
    pro = pro_features()

    def respond():
        dict_project = add_custom_contrib_button_to(project, get_user_id_or_ip(), ps=ps)
        response = dict(template='projects/new_blogpost.html',
                        title=gettext("Write a new post"),
                        form=form,
                        project=project_sanitized,
                        owner=owner_sanitized,
                        overall_progress=ps.overall_progress,
                        n_tasks=ps.n_tasks,
                        n_task_runs=ps.n_task_runs,
                        n_completed_tasks=cached_projects.n_completed_tasks(dict_project.get('id')),
                        n_volunteers=cached_projects.n_volunteers(dict_project.get('id')),
                        pro_features=pro)
        return handle_content_type(response)

    project, owner, ps = project_by_shortname(short_name)


    form = BlogpostForm(request.form)
    del form.id

    project_sanitized, owner_sanitized = sanitize_project_owner(project, owner,
                                                                current_user,
                                                                ps)

    if request.method != 'POST':
        ensure_authorized_to('create', Blogpost, project_id=project.id)
        return respond()

    if not form.validate():
        flash(gettext('Please correct the errors'), 'error')
        return respond()

    blogpost = Blogpost(title=form.title.data,
                        body=form.body.data,
                        user_id=current_user.id,
                        project_id=project.id)
    ensure_authorized_to('create', blogpost)
    blog_repo.save(blogpost)
    cached_projects.delete_project(short_name)

    msg_1 = gettext('Blog post created!')
    flash(Markup('<i class="icon-ok"></i> {}').format(msg_1), 'success')

    return redirect(url_for('.show_blogposts', short_name=short_name))


@blueprint.route('/<short_name>/<int:id>/update', methods=['GET', 'POST'])
@login_required
def update_blogpost(short_name, id):

    project, owner, ps = project_by_shortname(short_name)

    pro = pro_features()
    blogpost = blog_repo.get_by(id=id, project_id=project.id)
    if blogpost is None:
        raise abort(404)

    def respond():
        return render_template('projects/update_blogpost.html',
                               title=gettext("Edit a post"),
                               form=form, project=project, owner=owner,
                               blogpost=blogpost,
                               overall_progress=ps.overall_progress,
                               n_task_runs=ps.n_task_runs,
                               n_completed_tasks=cached_projects.n_completed_tasks(project.id),
                               n_volunteers=cached_projects.n_volunteers(project.id),
                               pro_features=pro)

    form = BlogpostForm()

    if request.method != 'POST':
        ensure_authorized_to('update', blogpost)
        form = BlogpostForm(obj=blogpost)
        return respond()

    if not form.validate():
        flash(gettext('Please correct the errors'), 'error')
        return respond()

    ensure_authorized_to('update', blogpost)
    blogpost = Blogpost(id=form.id.data,
                        title=form.title.data,
                        body=form.body.data,
                        user_id=current_user.id,
                        project_id=project.id,
                        published=form.published.data)
    blog_repo.update(blogpost)
    cached_projects.delete_project(short_name)

    msg_1 = gettext('Blog post updated!')
    flash(Markup('<i class="icon-ok"></i> {}').format(msg_1), 'success')

    return redirect(url_for('.show_blogposts', short_name=short_name))


@blueprint.route('/<short_name>/<int:id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_blogpost(short_name, id):
    project = project_by_shortname(short_name)[0]
    blogpost = blog_repo.get_by(id=id, project_id=project.id)
    if blogpost is None:
        raise abort(404)

    ensure_authorized_to('delete', blogpost)
    blog_repo.delete(blogpost)
    cached_projects.delete_project(short_name)
    msg_1 = gettext('Blog post deleted!')
    flash(Markup('<i class="icon-ok"></i> {}').format(msg_1), 'success')
    return redirect(url_for('.show_blogposts', short_name=short_name))


def _check_if_redirect_to_password(project):
    cookie_exp = current_app.config.get('PASSWD_COOKIE_TIMEOUT')
    passwd_mngr = ProjectPasswdManager(CookieHandler(request, signer, cookie_exp))
    if passwd_mngr.password_needed(project, get_user_id_or_ip()):
        return redirect_content_type(url_for('.password_required',
                                short_name=project.short_name, next=request.path))


@blueprint.route('/<short_name>/auditlog')
@login_required
def auditlog(short_name):
    pro = pro_features()
    if not pro['auditlog_enabled']:
        raise abort(403)

    project, owner, ps = project_by_shortname(short_name)


    ensure_authorized_to('read', Auditlog, project_id=project.id)
    logs = auditlogger.get_project_logs(project.id)
    project = add_custom_contrib_button_to(project, get_user_id_or_ip(), ps=ps)
    return render_template('projects/auditlog.html', project=project,
                           owner=owner, logs=logs,
                           overall_progress=ps.overall_progress,
                           n_tasks=ps.n_tasks,
                           n_task_runs=ps.n_task_runs,
                           n_completed_tasks=ps.n_completed_tasks,
                           n_volunteers=ps.n_volunteers,
                           pro_features=pro)


@blueprint.route('/<short_name>/publish', methods=['GET', 'POST'])
@login_required
def publish(short_name):

    project, owner, ps = project_by_shortname(short_name)

    pro = pro_features()
    ensure_authorized_to('publish', project)
    if request.method == 'GET':
        return render_template('projects/publish.html',
                                project=project,
                                pro_features=pro)
    project.published = True
    project_repo.save(project)
    task_repo.delete_taskruns_from_project(project)
    result_repo.delete_results_from_project(project)
    webhook_repo.delete_entries_from_project(project)
    cached_projects.delete_project(short_name)
    auditlogger.log_event(project, current_user, 'update', 'published', False, True)
    flash(gettext('Project published! Volunteers will now be able to help you!'))
    return redirect(url_for('.details', short_name=project.short_name))


def project_event_stream(short_name, channel_type):
    """Event stream for pub/sub notifications."""
    pubsub = sentinel.master.pubsub()
    channel = "channel_%s_%s" % (channel_type, short_name)
    pubsub.subscribe(channel)
    for message in pubsub.listen():
        yield 'data: %s\n\n' % message['data']


@blueprint.route('/<short_name>/privatestream')
@login_required
def project_stream_uri_private(short_name):
    """Returns stream."""
    if current_app.config.get('SSE'):
        project, owner, ps = project_by_shortname(short_name)
        if (current_user.id in project.owners_ids or current_user.admin):
            return Response(project_event_stream(short_name, 'private'),
                            mimetype="text/event-stream",
                            direct_passthrough=True)
        else:
            return abort(403)
    else:
        return abort(404)


@blueprint.route('/<short_name>/publicstream')
def project_stream_uri_public(short_name):
    """Returns stream."""
    if current_app.config.get('SSE'):
        project, owner, ps = project_by_shortname(short_name)
        return Response(project_event_stream(short_name, 'public'),
                        mimetype="text/event-stream")
    else:
        abort(404)


@blueprint.route('/<short_name>/webhook', defaults={'oid': None})
@blueprint.route('/<short_name>/webhook/<int:oid>', methods=['GET', 'POST'])
@login_required
def webhook_handler(short_name, oid=None):
    project, owner, ps = project_by_shortname(short_name)

    pro = pro_features()
    if not pro['webhooks_enabled']:
        raise abort(403)

    responses = webhook_repo.filter_by(project_id=project.id)
    if request.method == 'POST' and oid:
        tmp = webhook_repo.get(oid)
        if tmp:
            webhook_queue.enqueue(webhook, project.webhook,
                                  tmp.payload, tmp.id)
            return json.dumps(tmp.dictize())
        else:
            abort(404)


    ensure_authorized_to('read', Webhook, project_id=project.id)
    redirect_to_password = _check_if_redirect_to_password(project)
    if redirect_to_password:
        return redirect_to_password

    if request.method == 'GET' and request.args.get('all'):
        for wh in responses:
            webhook_queue.enqueue(webhook, project.webhook,
                                  wh.payload, wh.id)
        flash('All webhooks enqueued')

    if request.method == 'GET' and request.args.get('failed'):
        for wh in responses:
            if wh.response_status_code != 200:
                webhook_queue.enqueue(webhook, project.webhook,
                                      wh.payload, wh.id)
        flash('All webhooks enqueued')

    project = add_custom_contrib_button_to(project, get_user_id_or_ip(), ps=ps)

    return render_template('projects/webhook.html', project=project,
                           owner=owner, responses=responses,
                           overall_progress=ps.overall_progress,
                           n_tasks=ps.n_tasks,
                           n_task_runs=ps.n_task_runs,
                           n_completed_tasks=ps.n_completed_tasks,
                           n_volunteers=ps.n_volunteers,
                           pro_features=pro)


@blueprint.route('/<short_name>/results')
def results(short_name):
    """Results page for the project."""

    project, owner, ps = project_by_shortname(short_name)

    title = project_title(project, "Results")

    ensure_authorized_to('read', project)

    pro = pro_features()

    title = project_title(project, None)
    project = add_custom_contrib_button_to(project, get_user_id_or_ip(), ps=ps)

    project_sanitized, owner_sanitized = sanitize_project_owner(project, owner,
                                                                current_user,
                                                                ps)

    template_args = {"project": project_sanitized,
                     "title": title,
                     "owner": owner_sanitized,
                     "n_tasks": ps.n_tasks,
                     "n_task_runs": ps.n_task_runs,
                     "overall_progress": ps.overall_progress,
                     "last_activity": ps.last_activity,
                     "n_completed_tasks": ps.n_completed_tasks,
                     "n_volunteers": ps.n_volunteers,
                     "pro_features": pro,
                     "n_results": ps.n_results}

    response = dict(template = '/projects/results.html', **template_args)

    return handle_content_type(response)


@blueprint.route('/<short_name>/resetsecretkey', methods=['POST'])
@login_required
def reset_secret_key(short_name):
    """
    Reset Project key.

    """

    project, owner, ps = project_by_shortname(short_name)


    title = project_title(project, "Results")

    ensure_authorized_to('update', project)

    project.secret_key = make_uuid()
    project_repo.update(project)
    cached_projects.delete_project(short_name)
    msg = gettext('New secret key generated')
    flash(msg, 'success')
    return redirect_content_type(url_for('.update', short_name=short_name))


@blueprint.route('/<short_name>/coowners', methods=['GET', 'POST'])
@login_required
def coowners(short_name):
    """Manage coowners of a project."""
    form = SearchForm(request.form)
    project = project_repo.get_by_shortname(short_name)
    owners = user_repo.get_users(project.owners_ids)
    pub_owners = [user.to_public_json() for user in owners]
    for owner, p_owner in zip(owners, pub_owners):
        if owner.id == project.owner_id:
            p_owner['is_creator'] = True

    ensure_authorized_to('read', project)
    ensure_authorized_to('update', project)

    response = dict(
        template='/projects/coowners.html',
        project=project.to_public_json(),
        coowners=pub_owners,
        title=gettext("Manage Co-owners"),
        form=form,
        pro_features=pro_features()
    )

    if request.method == 'POST' and form.user.data:
        query = form.user.data

        filters = {'enabled': True}
        users = user_repo.search_by_name(query, **filters)

        if not users:
            markup = Markup('<strong>{}</strong> {} <strong>{}</strong>')
            flash(markup.format(gettext("Ooops!"),
                                gettext("We didn't find any enabled user matching your query:"),
                                form.user.data))
        else:
            found = []
            for user in users:
                public_user = user.to_public_json()
                public_user['is_coowner'] = user.id in project.owners_ids
                public_user['is_creator'] = user.id == project.owner_id
                found.append(public_user)
            response['found'] = found

    return handle_content_type(response)


@blueprint.route('/<short_name>/add_coowner/<user_name>')
@login_required
def add_coowner(short_name, user_name=None):
    """Add project co-owner."""
    project = project_repo.get_by_shortname(short_name)
    user = user_repo.get_by_name(user_name)

    ensure_authorized_to('read', project)
    ensure_authorized_to('update', project)

    if project and user:
        if user.id in project.owners_ids:
            flash(gettext('User is already an owner'), 'warning')
        else:
            project.owners_ids.append(user.id)
            project_repo.update(project)
            flash(gettext('User was added to list of owners'), 'success')
        return redirect_content_type(url_for(".coowners", short_name=short_name))
    return abort(404)


@blueprint.route('/<short_name>/del_coowner/<user_name>')
@login_required
def del_coowner(short_name, user_name=None):
    """Delete project co-owner."""
    project = project_repo.get_by_shortname(short_name)
    user = user_repo.get_by_name(user_name)

    ensure_authorized_to('read', project)
    ensure_authorized_to('update', project)

    if project and user:
        if user.id == project.owner_id:
            flash(gettext('Cannot remove project creator'), 'error')
        elif user.id not in project.owners_ids:
            flash(gettext('User is not a project owner'), 'error')
        else:
            project.owners_ids.remove(user.id)
            project_repo.update(project)
            flash(gettext('User was deleted from the list of owners'),
                  'success')
        return redirect_content_type(url_for('.coowners', short_name=short_name))
    return abort(404)


@blueprint.route('/<short_name>/tasks/projectreport/export')
@login_required
def export_project_report(short_name):
    """Export individual project information in the given format"""
    project, owner, ps = allow_deny_project_info(short_name)

    def respond_csv(ty):
        if ty not in ('project',):
            return abort(404)

        try:
            res = project_report_csv_exporter.response_zip(project, ty)
            return res
        except Exception as e:
            current_app.logger.exception(
                    'CSV Export Failed - Project: {0}, Type: {1} - Error: {2}'
                    .format(project.short_name, ty, e))
            flash(gettext('There was an error while exporting your data.'),
                  'error')
        return abort(500)

    export_formats = ['csv']
    ty = request.args.get('type')
    fmt = request.args.get('format')

    if not (fmt and ty):
        if len(request.args) >= 1:
            abort(404)
        project = add_custom_contrib_button_to(project, get_user_id_or_ip())
        return respond()

    if fmt not in export_formats:
        abort(415)

    if ty == 'project':
        project = project_repo.get(project.id)
        if project:
            ensure_authorized_to('read', project)

    return {'csv': respond_csv}[fmt](ty)


@blueprint.route('/<short_name>/syncproject', methods=['POST'])
@login_required
@admin_or_subadmin_required
def sync_project(short_name):
    """Sync project."""
    project, owner, ps = project_by_shortname(short_name)
    title = project_title(project, "Sync")

    sync_form = ProjectSyncForm()
    target_url = sync_form.target_url.data
    target_key = sync_form.target_key.data

    synced_url = '{}/project/{}'.format(
        target_url, project.short_name)
    success_msg = Markup(
        '{} <strong><a href="{}" target="_blank">{}</a></strong>')
    success_body = (
        'A project that you an owner/co-owner of has been'
        ' {action}ed with a project on another server.\n\n'
        '    Project Short Name: {short_name}\n'
        '    Source URL: {source_url}\n'
        '    User who performed sync: {syncer}')

    try:
        project_syncer = ProjectSyncer()
        source_url = current_app.config.get('SERVER_URL')

        # Ensure fields are passed
        if not target_url or not target_key:
            msg = Markup('Enter a <strong>Target URL</strong> and'
                         ' an <strong>API Key</strong> to sync'
                         ' your project')
            flash(msg, 'error')

            return redirect_content_type(
                url_for('.update', short_name=short_name))

        if request.form.get('btn') == 'sync':
            action = 'sync'
            res = project_syncer.sync(
                project, target_url, target_key, current_user)
        elif request.form.get('btn') == 'undo':
            action = 'unsync'
            res = project_syncer.undo_sync(
                project, target_url, target_key)

        # Nothing to revert
        if not res and action == 'unsync':
            msg = gettext('There is nothing to revert.')
            flash(msg, 'warning')
        # Success
        elif res.ok:
            if action == 'sync':
                sync_msg = gettext('Project sync completed! ')
                subject = 'Your project has been synced'
            elif action == 'unsync':
                sync_msg = gettext('Last sync has been reverted! ')
                subject = 'Your synced project has been reverted'

            msg = success_msg.format(
                sync_msg, synced_url, 'Synced Project Link')
            flash(msg, 'success')

            body = success_body.format(
                action=action,
                short_name=project.short_name,
                source_url=source_url,
                syncer=current_user.email_addr)
            owners = project_syncer.get_target_owners(
                project, target_url, target_key)
            email = dict(recipients=owners,
                         subject=subject,
                         body=body)
            mail_queue.enqueue(send_mail, email)
        # Failure
        else:
            current_app.logger.error(
                'A request error occurred while syncing {}: {}'
                .format(project.short_name, res.reason))
            msg = gettext(
                'The target server returned an error.  '
                'Check the Target API Key.')
            flash(msg, 'error')
    except NotEnabled:
        msg = gettext(
            'The target project has not been enabled for syncing.')
        flash(msg, 'error')
    except Exception:
        current_app.logger.exception(
            'An error occurred while syncing {}'
            .format(project.short_name))
        msg = gettext(
            'An error occurred while trying to reach your target.'
            '  Check the Target URL and Target API Key fields.')
        flash(msg, 'error')

    return redirect_content_type(
        url_for('.update', short_name=short_name))
