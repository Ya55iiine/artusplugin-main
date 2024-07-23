# -*- coding: utf-8 -*-
#
# Copyright (C) 2016 Artus
# All rights reserved.
#
# Author: Michel Guillot <michel.guillot@meggitt.com>

""" Panels for managing Version Tags and Milestone tags. """

# Python-Future
from __future__ import print_function


# Genshi
from genshi.builder import tag
from genshi.filters.transform import Transformer

# Trac
from trac.admin import IAdminPanelProvider
from trac.attachment import AttachmentModule
from trac.core import Component as CComponent, implements, TracError
from trac.mimeview import Context
from trac.perm import IPermissionRequestor
from trac.resource import ResourceNotFound
from trac.ticket import Ticket
from trac.ticket.model import Version, Milestone, Component, Type
from trac.ticket.admin import VersionAdminPanel, MilestoneAdminPanel
from trac.timeline.api import ITimelineEventProvider
from trac.util.compat import partial
from trac.util.datefmt import utc, format_datetime, \
    to_utimestamp, from_utimestamp
from trac.util.text import exception_to_unicode, unicode_quote, unicode_from_base64
from trac.versioncontrol.api import NoSuchNode
from trac.web import IRequestHandler
from trac.web.api import ITemplateStreamFilter, IRequestFilter, parse_arg_list
from trac.web.auth import LoginModule
from trac.web.chrome import add_script, add_notice, add_ctxtnav, \
    Chrome, add_warning, add_stylesheet
from trac.web.href import Href
from trac.ticket.web_ui import TicketModule

# Standard lib
# import ConfigParser
from backports import configparser as ConfigParser
import codecs
from datetime import datetime
# from ldap_utilities import Ldap_Utilities
from artusplugin.ldap.ldap_utilities import Ldap_Utilities
from time import sleep
import fileinput
from collections import MutableMapping, OrderedDict
import glob
import json
import operator
import os
import re
import shutil
import signal
import subprocess
import sys
import syslog
import warnings

from urllib.parse import unquote
from xml.dom.minidom import parse

# ODFPY
from odf.opendocument import load
from odf.table import Table

# Announcer Plugin
from artusplugin.announcer.specified import SpecifiedEmailResolver

# Same package
from artusplugin import util, model, Ooo, cache, web_ui, _, N_, tag_
from artusplugin.buildbot.model import Build
from artusplugin.buildbot.web_ui import BuildBotModule
from artusplugin.cache import Ticket_Cache
from artusplugin.form import TicketFormTemplate, MSOFormTemplate
from artusplugin.model import NamingRule
from artusplugin.util import OrderedSet

# Profiling
# import hotshot

# Constants
SCI_LIST_DIRECTORY = '/tmp/.SCI-list'
SCI_LIST_TEMPLATE = 'SCI-list.odt'
REQTIFY_PROJECT_DIRECTORY = '/tmp/.reqtify-project'
PACKAGE_LIST_DIRECTORY = '/tmp/.list-get'
TARGET_TABLE = 'Tableau1'
ROW_CELLS = ['A2', 'B2']
TARGET_CELLS = {"A2": 'Revision', "B2": 'Component'}
STATUS_LIST = {'document': {'Draft': 1, 'Proposed': 2, 'Released': 3},
               'component': {'Engineering': 1, 'Candidate': 2,
                             'Released': 3, 'Patch': 4},
               'milestone': {'Prepared': 1, 'Reviewed': 2, 'Accepted': 3}
               }


def get_row_style(name, args):
    """ When coming from detailed view
        highlights the associated item """
    if name == args.get('selected_item'):
        return [('style', 'background-color:#eeeedd')]
    else:
        return []


def get_sorted_tags_by_rev(tags):
    return sorted(tags, key=lambda tg: int(util.get_revision(tg.tag_url)))


def get_applied_tags(env, item, item_type):
    """
    item: tracked_item (document, component) or tagged_item (milestone)
    item_type: 'document', 'component' or 'milestone'
    version_type: 'er' (document), 'ser' or 'ma' (component), None (milestone)
    """

    if item_type == 'milestone':
        applied_tags = [v for v in model.Tag.select(env,
                        ['tagged_item = "%s"' % item,
                         'tag_url IS NOT NULL'],
                        ordering_term='rev ASC',
                        tag_type=MilestoneTagsAdminPanel.page_type)]

    else:
        applied_tags = [v for v in model.Tag.select(env,
                        ['tracked_item = "%s"' % item,
                         'tag_url IS NOT NULL'],
                        ordering_term='rev ASC',
                        tag_type=VersionTagsAdminPanel.page_type)]

    return applied_tags


def get_docs_from_including_tag(env, including_tag_name, seen_doc_versions,
                                seen_including_tags, including_subpath=None, external_refs=False):
    """
    Explores a given baseline or milestone
    and all the included baselines or milestones recursively
    and list all document versions found.
    Each baseline or milestone version is explored only once.
    """

    included_tag_names = []
    including_tag = model.Tag(env, including_tag_name)

    if including_tag.review is not None or including_tag.baselined == 1:
        # Baseline or milestone
        if including_tag.name not in [sit[0] for sit in seen_including_tags]:
            seen_including_tags.add((including_tag.name, including_subpath))
            included_tag_names = [v.name for v in model.BaselineItem.select(env,
                                  ['baselined_tag="' + including_tag.name + '"'])]
            if external_refs and including_tag.tag_refs:
                # Include external references
                included_tag_names += including_tag.tag_refs.splitlines()

    for included_tag_name in included_tag_names:
        try:
            included_tag = model.Tag(env, included_tag_name)
        except Exception:
            continue
        try:
            v = model.BaselineItem(env, (included_tag_name, including_tag_name))
            if including_subpath:
                subpath = '%s/%s%s' % (including_subpath, including_tag_name,
                                       v.subpath)
            else:
                subpath = v.subpath
            if subpath:
                subpath = subpath.rstrip('/')
        except Exception:
            subpath = None
        if included_tag.review is not None or included_tag.baselined == 1:
            # Baseline or milestone
            seen_doc_versions, seen_including_tags = \
                get_docs_from_including_tag(env, included_tag.name,
                                            seen_doc_versions,
                                            seen_including_tags, subpath, external_refs)
        elif not included_tag.component:
            # Document
            if included_tag.name not in [sdv[0] for sdv in seen_doc_versions]:
                seen_doc_versions.add((included_tag.name, subpath))

    return seen_doc_versions, seen_including_tags


def get_change_options(env, applied_tag):
    """
    Returns:
     '+' if there is a newer applied tag than the one given
     '+-' if there is a newer and an older applied tag than the one given
     '-' if there is an older applied tag than the one given
     '' if there is no newer nor older applied tag than the one given
    """
    options = ''

    if applied_tag.review:
        item_type = 'milestone'
        item = applied_tag.tagged_item
    elif applied_tag.component:
        item_type = 'component'
        item = applied_tag.tracked_item
    else:
        item_type = 'document'
        item = applied_tag.tracked_item

    applied_tags = get_applied_tags(env, item, item_type)

    if item_type != 'milestone':
        branch_segregation_activated = True if env.config.get('artusplugin', 'branch_segregation_activated') == 'True' else False
        if branch_segregation_activated:
            branch_name = NamingRule.get_branch_from_tag(env, applied_tag.name)
            applied_tags = [tg for tg in applied_tags if NamingRule.get_branch_from_tag(env, tg.name) == branch_name]

    idx = [tg.name for tg in applied_tags].index(applied_tag.name)
    tagslength = len(applied_tags)
    if tagslength == 1:
        options = ''
    elif idx == tagslength - 1:
        options = '-'
    elif idx == 0:
        options = '+'
    else:
        options = '+-'

    return options


def init_data(self, req):
    data = {}

    # environment & args
    data['env'] = self.env
    data['args'] = req.args

    program_data = util.get_program_data(self.env)
    data['base_path'] = program_data['base_path']
    data['trac_env_name'] = program_data['trac_env_name']
    data['program_name'] = program_data['program_name']

    data['get_tracbrowserurl'] = util.get_tracbrowserurl
    data['get_revision'] = util.get_revision
    data['get_url'] = util.get_url
    data['perm'] = req.perm
    data['get_skill'] = util.get_skill
    data['get_ecm_tktid'] = util.get_ecm_tktid
    data['get_ecm_tktstatus'] = util.get_ecm_tktstatus
    data['get_fee_tktid'] = util.get_fee_tktid
    data['get_fee_tktstatus'] = util.get_fee_tktstatus
    data['get_doc_tktid'] = util.get_doc_tktid
    data['get_doc_tktstatus'] = util.get_doc_tktstatus
    data['get_doc_skill'] = util.get_doc_skill
    data['get_mom_tktid'] = util.get_mom_tktid
    data['get_mom_tktstatus'] = MilestoneTagsAdminPanel.get_mom_tktstatus
    data['get_mom_skill'] = MilestoneTagsAdminPanel.get_mom_skill
    data['is_milestone_accepted'] = MilestoneTagsAdminPanel.is_milestone_accepted
    data['get_branch_from_tag'] = NamingRule.get_branch_from_tag

    # X & Y scrolling position
    if req.args.get('ScrollX'):
        data['ScrollX'] = req.args.get('ScrollX')
    else:
        data['ScrollX'] = 1

    if req.args.get('ScrollY'):
        data['ScrollY'] = req.args.get('ScrollY')
    else:
        data['ScrollY'] = 1

    # Recently added version style
    data['get_row_style'] = get_row_style

    # Selected Item
    if req.args.get('selected_item'):
        data['selected_item'] = req.args.get('selected_item')
    else:
        data['selected_item'] = None

    def get_tag(tag_name):
        if tag_name:
            v = model.Tag(self.env, name=tag_name)
        else:
            v = None
        return v

    data['get_tag'] = get_tag

    # Header used for sorting 'including' type table
    if req.args.get('sort_including') and req.args.get('asc_including'):
        data['sort_including'] = req.args.get('sort_including')
        data['asc_including'] = req.args.get('asc_including')
    else:
        data['sort_including'] = 'name'
        data['asc_including'] = '1'

    # Associated ordering term for SQL request
    data['ordering_term_including'] = data['sort_including']
    if data['asc_including'] == '1':
        data['ordering_term_including'] += ' ASC'
    else:
        data['ordering_term_including'] += ' DESC'

    # Header used for sorting 'included' type table
    if req.args.get('sort_included') and req.args.get('asc_included'):
        data['sort_included'] = req.args.get('sort_included')
        data['asc_included'] = req.args.get('asc_included')
    else:
        data['sort_included'] = 'name'
        data['asc_included'] = '1'

    # Associated ordering term for SQL request
    data['ordering_term_included'] = data['sort_included']
    if data['asc_included'] == '1':
        data['ordering_term_included'] += ' ASC'
    else:
        data['ordering_term_included'] += ' DESC'

    # Including Baselines
    def get_including_baselines(tag_name):
        if tag_name:
            including_baselines = [including_baseline for including_baseline
                                   in model.BaselineItem.select(
                                       self.env,
                                       ['name="' + tag_name + '"'],
                                       ordering_term=data[
                                           'ordering_term_including'])]
        else:
            including_baselines = []
        return including_baselines

    data['get_including_baselines'] = get_including_baselines

    # User defined filter
    if 'filter_value' in req.args:
        data['filter_value'] = req.args.get('filter_value')
    else:
        data['filter_value'] = _('Set the appropriate filter')

    # Baseline tags
    def get_baseline_tags(tag_name):
        if tag_name:
            baseline_tags = [baseline_tag
                             for baseline_tag in model.BaselineItem.select(
                                 self.env, ['baselined_tag="' + tag_name + '"'],
                                 ordering_term=data['ordering_term_included'])]
        else:
            baseline_tags = []
        return baseline_tags

    data['get_baseline_tags'] = get_baseline_tags

    def get_panel_href(review=None):
        if review is None:
            return partial(req.href, 'admin', VersionTagsAdminPanel.cat_type,
                           VersionTagsAdminPanel.page_type)
        else:
            return partial(req.href, 'admin', MilestoneTagsAdminPanel.cat_type,
                           MilestoneTagsAdminPanel.page_type)

    data['get_panel_href'] = get_panel_href

    # Node data for tag rev date display through title
    def get_formatted_datetime(date):
        display_date = format_datetime(date, 'iso8601', req.tz)
        fmt = req.session.get('datefmt')
        if fmt and fmt != 'iso8601':
            display_date = format_datetime(date, fmt, req.tz)
        return display_date

    data['get_formatted_datetime'] = get_formatted_datetime

    data['get_repository'] = util.get_repository

    # Get log message
    def get_log_message(tag_url):
        """
        tag_url : [reponame]/[trunk|branches|tags]/.../tag_name?rev=...
        """
        changeset = util.get_repository(self.env,
                                        tag_url).get_changeset(util.get_revision(tag_url))
        return changeset.message.replace('\n', ' ')

    data['get_log_message'] = get_log_message

    # Version description
    def get_version_description(vtag_name):
        tagged_item = get_tag(vtag_name).tagged_item
        try:
            w = Version(self.env, name=tagged_item)
            description = w.description
        except ResourceNotFound:
            description = ''
        return description

    data['get_version_description'] = get_version_description

    # Version tag description
    def get_vtag_description(vtag):
        description = get_version_description(vtag.name)
        if vtag.tag_url:
            if description:
                description += ' - '
            description += get_log_message(vtag.tag_url)
        return description

    data['get_vtag_description'] = get_vtag_description

    # Milestone description
    def get_milestone_description(mtag_name):
        tagged_item = get_tag(mtag_name).tagged_item
        try:
            w = Milestone(self.env, name=tagged_item)
            description = w.description
        except ResourceNotFound:
            description = ''
        return description

    data['get_milestone_description'] = get_milestone_description

    # Milestone tag description
    def get_mtag_description(mtag):
        description = get_milestone_description(mtag.name)
        if mtag.tag_url:
            if description:
                description += ' - '
            description += get_log_message(mtag.tag_url)
        return description

    data['get_mtag_description'] = get_mtag_description

    # "Normalize" the url for comparing trunk and branch and buildbot urls
    def normalize(url_in):
        skill_options = self.env.config.get('ticket-custom', 'skill.options')
        re_document = r"\A" + data['program_name'] + r"_(?:%s)_(?:[^\W_]+(?:-?(?:(?<=-)[^\W_]+|(?<!-)(?=_)))*_)+[1-9]\d*\.(?:0|[1-9]\d*)\.(?:Draft[1-9]\d*|Proposed[1-9]\d*|Released)\Z" % skill_options
        re_component = r"\A" + data['program_name'] + r"_(?:%s)_(?:[^\W_]+(?:-?(?:(?<=-)[^\W_]+|(?<!-)(?=_)))*_)+\d\d\.\d\d\.\d\d[ECRP]\d\d\Z" % skill_options
        in_dirs = url_in.split('/')
        out_dirs = [in_dir for in_dir in in_dirs if
                    in_dir != '' and
                    in_dir not in ('trunk', 'branches') and
                    in_dir not in ('Draft', 'Proposed', 'Released') and
                    in_dir not in ('Engineering', 'Candidate', 'Draft', 'Proposed') and
                    not re.search(re_document, in_dir, re.UNICODE) and
                    not re.search(re_component, in_dir, re.UNICODE) and
                    in_dir not in ('prod', 'check', 'build')]
        url_out = '/'.join(out_dirs)
        return url_out

    data['normalize'] = normalize

    # For a given tag, tells if there is a newer and/or an older one
    data['get_change_options'] = get_change_options

    return data


def browse_for_files_in_repo(self, req, repo_url):
    repos = util.get_repository(self.env, repo_url)
    repo_path = util.get_url(repo_url)
    filenodes = []
    try:
        node = repos.get_node(repo_path)
        if node.isfile:
            filenodes.append(node)
        else:  # recurse on node.get_entries
            for childnode in node.get_entries():
                filenodes += browse_for_files_in_repo(self, req, childnode.path)
    except NoSuchNode:
        pass  # ignore broken repositories used for testing
    except Exception as e:
        raise TracError(e)
    return filenodes


def generate_tag_index(env, tag_name, tag_url, program_name, authname):
    """ Generates a OOo listing of the tag_name @ tag_url and return its file path """

    # Creates SCI_LIST_DIRECTORY if it does not exist
    try:
        pid = os.getpid()
        tmp_dir = '%s/%s' % (SCI_LIST_DIRECTORY, pid)
        os.makedirs(tmp_dir)
    except Exception:
        pass

    # Retrieve the empty SCI list form

    filepath = '%s/%s.odt' % (tmp_dir, tag_name)
    shutil.copy('%s/%s' % (Chrome(env).get_templates_dirs()[1],
                           SCI_LIST_TEMPLATE), filepath)

    # Open the empty SCI list form
    doc = load(filepath)

    # Retrieves tag entries from the repository
    tg_idx_name = '%s/tag_index.xml' % tmp_dir
    unix_cmd = util.SVN_TEMPLATE_CMD % {
        'subcommand':
        'list --xml --recursive'} + '"' + tag_url + '" > ' + tg_idx_name
    os.system(unix_cmd)
    dom = parse(tg_idx_name)

    # Put data into an OOo table
    entry_data = {}
    entry_data['Revision'] = {}
    entry_data['Revision']['link'] = None
    entry_data['Component'] = {}
    entry_data['Component']['link'] = None

    for table in doc.getElementsByType(Table):
        if table.getAttribute("name") == TARGET_TABLE:
            # First table line (header being excluded)
            template_row = Ooo.get_table_rows(table)[0]
            for entry in dom.getElementsByTagName("entry"):
                entry_data['Revision']['text'] = entry.getElementsByTagName("commit")[0].getAttribute("revision")
                entry_data['Component']['text'] = entry.getElementsByTagName("name")[0].childNodes[0].data
                Ooo.add_row_to_table(table,
                                     template_row,
                                     ROW_CELLS,
                                     TARGET_CELLS,
                                     entry_data)
            break

    # Save the SCI list form
    Ooo.doc_save(doc, filepath)
    syslog.syslog("%s(%s): SCI list written" % (program_name, authname))

    return filepath


class ConfigDict(MutableMapping):
    """ """

    abstract = True

    def __init__(self, *args, **kwargs):
        self.store = dict()
        self.update(dict(*args, **kwargs))

    def __getitem__(self, key):
        return self.store[key]

    def __setitem__(self, key, value):
        self.store[key] = value

    def __delitem__(self, key):
        del self.store[key]

    def __iter__(self):
        return iter(sorted(self.store.keys(), key=self.mysort))

    def __len__(self):
        return len(self.store)


class AdminInterface(CComponent):
    """Web administration interface filters."""

    implements(ITemplateStreamFilter, IRequestFilter,
               IRequestHandler, ITimelineEventProvider)

    def __init__(self):
        CComponent.__init__(self)
        program_data = util.get_program_data(self.env)
        self.trac_env_name = program_data['trac_env_name']
        self.program_name = program_data['program_name']

    # ITemplateStreamFilter methods

    def filter_stream(self, req, method, filename, stream, data):
        """ The modifications applied to the TRAC browser for admin panels """

        # overlay
        add_stylesheet(req, self.env.config.get('trac', 'jquery_ui_theme_location'))

        if filename == "browser.html":

            href = data['path_links'][-1]['href']
            if not util.node_is_dir(
                    self.env,
                    util.repo_url(href)):
                return stream

            repo_names = self.env.config.get('artusplugin', 'conf_mgmt.repo_names')
            skills = self.env.config.get('ticket-custom',
                                         'skill.options').split('|')
            ticket_suffixes = util.get_prop_values(self.env, 'ticket_suffix')
            re_ticket_types = [ttype.name for ttype in Type.select(self.env)
                               if ttype.name in ticket_suffixes]
            re_frmtpl = r"\A(%s)_(%s)\Z" % ('|'.join(skills), '|'.join(re_ticket_types))

            def branch_segregation_notification_display(stream, button_label):
                stream |= Transformer('//div[@id="anydiff"]/form/div[@class="buttons"]').after(
                    tag.div(tag.p(tag.strong('Note: '),
                        _("Choose another branch with an id greater or equal to the <branch_segregation_first_branch> parameter value "
                        "to gain access to the <%s> button" % button_label),
                        id_='help', style_='text-align: left')))
                return stream

            if 'caller' in req.args and isinstance(req.args.get('caller'), str) and re.search(re_frmtpl, req.args.get('caller')):

                if 'MILESTONE_CREATE' in req.perm:

                    # 'Back to Form Templates...'
                    selected_url = util.get_url(util.repo_url(href))
                    if selected_url:
                        repos = util.get_repository(self.env, selected_url)
                        if repos:
                            repo_path = util.repo_path(selected_url)
                            match = re.search(re_frmtpl, req.args.get('caller'))
                            ticket_skill = match.group(1)
                            ticket_type = match.group(2)
                            ticket_suffix = ticket_suffixes[ticket_type]
                            # Effective template filename
                            template_filename = MSOFormTemplate.get_template_filenames(
                                self.env, ticket_type, ticket_skill, ticket_suffix)[1]
                            node = '%s/%s' % (repo_path, template_filename)
                            revision = util.get_revision(href)
                            if not revision:
                                revision = util.get_head_revision(repos)
                            if revision:
                                if repos.has_node(node, revision):
                                    url = Href(self.env.base_url)
                                    url_form_template = url.admin('tags_mgmt',
                                                                  'templates',
                                                                  caller=req.args.get('caller'),
                                                                  selected_url='%s?rev=%s'
                                                                  % (selected_url, revision))
                                    stream |= Transformer('//div[@id="anydiff"]/form/div/input[@type="submit"]').after(
                                        tag.div(tag.input(value_="Back to Templates...",
                                                          name_='Form_Templates_view',
                                                          title_="Set form template(s) path and revision",
                                                          type_='button',
                                                          class_='buttons'),
                                                style_='margin-left:10px;'))
                                    stream |= Transformer('//input[@name="Form_Templates_view"]').attr(
                                        'onclick', 'location.href="%s"' % url_form_template)
                                else:
                                    stream |= Transformer('//div[@id="anydiff"]/form/div[@class="buttons"]').after(
                                        tag.div(tag.strong('Note: '),
                                                _("Browse to display the content of the directory with the expected template %s" % template_filename),
                                                id_='help', style_='margin-left: 1em;'))

            elif ('/tags/versions/' in req.path_info or
                  '/tags/milestones/' in req.path_info):
                # Version Tag buttons display management
                tagname = data['path_links'][-1]['name']

                if NamingRule.is_tag_dir(self.env, tagname, self.program_name,
                                         data['path_links'][-2]['name'],
                                         data['path_links'][-3]['name']):
                    if repo_names == "" or repo_names in req.path_info:
                        path = util.get_path(req.path_info)
                    else:
                        path = None

                    if path and path[1] == 'versions':

                        if 'VERSION_TAG_VIEW' in req.perm:
                            # 'Manage Version Tags...'
                            stream |= Transformer('//div[@id="anydiff"]/form/div/input[@type="submit"]').after(
                                tag.div(tag.input(value_="Manage Version Tags...",
                                                  name_='Version_Tag_view',
                                                  title_="Direct access to Version Tag data",
                                                  type_='button',
                                                  class_='buttons'),
                                        style_='margin-left:10px;'))
                            url = Href(self.env.base_url)
                            url_tag_view = url.admin('tags_mgmt',
                                                     'version_tags',
                                                     selected_item=tagname,
                                                     filter_value=tagname)
                            stream |= Transformer('//input[@name="Version_Tag_view"]').attr(
                                'onclick', 'location.href="%s"' % url_tag_view)

                            if 'caller' in req.args and isinstance(req.args.get('caller'), str):
                                caller = req.args.get('caller')
                                ci_name = model.Tag(self.env, tagname).tracked_item

                                if (caller.startswith(ci_name) and
                                    NamingRule.is_tag_name(self.env, caller, self.program_name)):
                                    # Source url
                                    regular_expression = (r"/tracs/%s/browser(/(?:\w+/)?"
                                                          r"%s[^?]*)(?:\?(.+))?" % (self.trac_env_name,
                                                                                    path[0]))
                                    source_url = util.url_from_browse(self.env,
                                                                      util.unicode_unquote_plus(href),
                                                                      regular_expression)
                                    # 'Back to version tag...'
                                    stream |= Transformer('//div[@id="anydiff"]/form/div/input[@type="submit"]').after(
                                        tag.div(tag.input(value_="Back to version tag ...",
                                                          name_='version_modification',
                                                          title_='Back to version tag %s' % caller,
                                                          type_='button',
                                                          class_='buttons'),
                                                style_='margin-left:10px;'))
                                    url = Href(self.env.base_url)
                                    url_version_modification = url.admin('tags_mgmt',
                                                                         'version_tags',
                                                                         caller,
                                                                         ci_source_url=source_url)
                                    stream |= Transformer('//input[@name="version_modification"]').attr(
                                        'onclick',
                                        'location.href="%s"' % url_version_modification)

                        if 'BRANCH_CREATE' in req.perm and 'caller' not in req.args:
                            stream |= Transformer('//div[@id="anydiff"]/form'
                                                  '/div/input[@type="submit"]').after(
                                tag.div(tag.input(value_="Create branch ...",
                                                  name_='branch_creation',
                                                  title_=("The branch will be created "
                                                          "on the selected tag "),
                                                  type_='button',
                                                  class_='buttons'),
                                        style_='margin-left:10px;'))
                            url = Href(self.env.base_url)
                            url_branch_creation = url.admin('tags_mgmt',
                                                            'branches',
                                                            ci_source_tag=tagname)
                            stream |= Transformer('//input[@name='
                                                  '"branch_creation"]').attr(
                                'onclick',
                                ('location.href="%s"' % url_branch_creation))

                    elif path and path[1] == 'milestones':

                        if 'MILESTONE_TAG_VIEW' in req.perm:
                            # 'Manage Milestone Tags...'
                            stream |= Transformer('//div[@id="anydiff"]/form/div/input[@type="submit"]').after(
                                tag.div(tag.input(value_="Manage Milestone Tags...",
                                                  name_='Milestone_Tag_view',
                                                  title_="Direct access to Milestone Tag data",
                                                  type_='button',
                                                  class_='buttons'),
                                        style_='margin-left:10px;'))
                            url = Href(self.env.base_url)
                            url_tag_view = url.admin('tags_mgmt',
                                                     'milestone_tags',
                                                     selected_item=tagname,
                                                     filter_value=tagname)
                            stream |= Transformer('//input[@name="Milestone_Tag_view"]').attr(
                                'onclick', 'location.href="%s"' % url_tag_view)

            elif ('/trunk/' in req.path_info or '/branches/' in req.path_info or
                  req.path_info.endswith('/trunk') or req.path_info.endswith('/branches')):
                # Version tag button display management
                if repo_names == "" or repo_names in req.path_info:
                    path = util.get_path(req.path_info)
                else:
                    path = None

                if path and path[0] in ('trunk', 'branches'):
                    dir_name = data['path_links'][-1]['name']

                    if NamingRule.is_ci_name(self.env, dir_name, self.program_name):
                        # This is a CI
                        version_type = None

                        # We first try to get the CI type
                        ci_type = ''

                        display_button = False

                        # Has this CI already been tagged ?
                        db = self.env.get_db_cnx()
                        cursor = db.cursor()
                        cursor.execute("SELECT DISTINCT tracked_item, component, version_type FROM tag "
                                       "WHERE tracked_item='%s'" % dir_name)
                        row = cursor.fetchone()
                        if row:
                            if row[1]:
                                ci_type = 'component'
                                version_type = 'ma' if row[2] else 'ser'
                            else:
                                ci_type = 'document'

                        # Is there a DOC ticket associated with this CI
                        doc_ticket = util.exist_doc_tktid(self.env, dir_name)

                        if doc_ticket:
                            ci_type = 'document'

                        # CI skill
                        skill = util.get_skill(self.env,
                                               dir_name,
                                               self.program_name)
                        # DOC skills
                        doc_skills = self.env.config.get('artusplugin',
                                                         'doc_skills', '').split('|')

                        # default ci-type
                        if not ci_type:
                            if skill not in doc_skills:
                                ci_type = 'document'
                            else:
                                ci_type = 'component'

                        regular_expression = (r"/tracs/%s/browser(/(?:\w+/)?"
                                              r"%s[^?]*)(?:\?(.+))?" % (self.trac_env_name,
                                                                        path[0]))
                        source_url = util.url_from_browse(self.env,
                                                          util.unicode_unquote_plus(href),
                                                          regular_expression)
                        revision = util.get_revision(source_url)

                        if (ci_type == 'component' or
                            skill not in doc_skills or
                            'VERSION_TAG_ADMIN' in data['perm']):
                            display_button = True

                        if ('VERSION_TAG_VIEW' in req.perm and
                            display_button and
                            'caller' in req.args and
                            isinstance(req.args.get('caller'), str)):
                            caller = req.args.get('caller')

                            if (caller.startswith(dir_name) and
                                NamingRule.is_tag_name(self.env, caller,
                                                       self.program_name)):
                                # 'Back to version tag...'
                                stream |= Transformer('//div[@id="anydiff"]/form'
                                                      '/div/input[@type="submit"]').after(
                                    tag.div(tag.input(value_=_("Back to version tag ..."),
                                                      name_='version_modification',
                                                      title_=_("Back to version tag %s" %
                                                              caller),
                                                      type_='button',
                                                      class_='buttons'),
                                            style_='margin-left:10px;'))
                                url = Href(self.env.base_url)
                                url_version_modification = url.admin(
                                    'tags_mgmt',
                                    'version_tags', caller,
                                    ci_source_url=source_url)
                                stream |= Transformer('//input[@name='
                                                      '"version_modification"]').attr(
                                    'onclick', ('location.href="%s"' %
                                                url_version_modification))

                        if 'VERSION_TAG_CREATE' in req.perm:
                            if display_button and 'caller' not in req.args:

                                branch_segregation_activated = True if self.env.config.get('artusplugin', 'branch_segregation_activated') == 'True' else False
                                if branch_segregation_activated:
                                    branch_segregation_first_branch = self.env.config.get('artusplugin', 'branch_segregation_first_branch', 'B1')

                                if not branch_segregation_activated or path[0] == 'trunk' or int(path[1][1:]) >= int(branch_segregation_first_branch[1:]):
                                    # 'Create version tag...'
                                    stream |= Transformer('//div[@id="anydiff"]/form'
                                                          '/div/input[@type="submit"]').after(
                                        tag.div(tag.input(value_="Create version tag ...",
                                                          name_='version_creation',
                                                          title_=("The version tag will be "
                                                                  "created on the selected "
                                                                  "directory and revision "
                                                                  "(HEAD or older)"),
                                                          type_='button',
                                                          class_='buttons'),
                                                style_='margin-left:10px;'))
                                    url = Href(self.env.base_url)
                                    url_version_creation = url.admin('tags_mgmt',
                                                                     'version_tags',
                                                                     ci_name=dir_name,
                                                                     ci_source_url= None if row else source_url,
                                                                     ci_type=ci_type,
                                                                     version_type=version_type)
                                    stream |= Transformer('//input[@name='
                                                          '"version_creation"]').attr(
                                        'onclick', 'location.href="%s"' % url_version_creation)
                                else:
                                    # Access to 'Create version tag' button
                                    stream = branch_segregation_notification_display(stream, 'Create version tag')

                            else:
                                DOC_report = self.env.config.get('artusplugin', 'DOC_report')
                                doc_query_string = util.get_doc_query_string(self.env, skill, dir_name)
                                if doc_query_string:
                                    stream |= Transformer('//div[@id="anydiff"]/form/div[@class="buttons"]').after(
                                        tag.div(tag.strong("Note: "),
                                                _("To create a new version tag on this document "
                                                "either create a DOC ticket or use an existing one - see report "),
                                                tag.a('{%s}' % DOC_report,
                                                      href=req.href('query') + doc_query_string,
                                                      title_=_("View all DOC tickets associated to this document"),
                                                      style_='text-align: left; margin: 0em'), id_='help', style_='margin-left: 1em;'))

                # Branch button display management
                if repo_names == "" or repo_names in req.path_info:
                    path = util.get_path(req.path_info)
                else:
                    path = None

                if path and path[0] in ('branches'):
                    branchname = path[1]
                    dir_name = data['path_links'][-1]['name']

                    if 'BRANCH_VIEW' in req.perm and branchname == dir_name:
                        branchsource = (model.Branch(self.env, branchname[1:]).source_url or
                                        model.Branch(self.env, branchname[1:]).source_tag)

                        # 'Manage Branches...'
                        stream |= Transformer('//div[@id="anydiff"]/form'
                                              '/div/input[@type="submit"]').after(
                            tag.div(tag.input(value_="Manage Branches...",
                                              name_='Branch_view',
                                              title_="Direct access to Branch data",
                                              type_='button',
                                              class_='buttons'),
                                    style_='margin-left:10px;'))
                        url = Href(self.env.base_url)
                        url_branch_view = url.admin('tags_mgmt',
                                                    'branches',
                                                    selected_item=branchname,
                                                    filter_value=branchsource)
                        stream |= Transformer('//input[@name='
                                              '"Branch_view"]').attr('onclick',
                                                                     'location.href="%s"' %
                                                                     url_branch_view)

                if path and path[0] in ('trunk', 'branches'):

                    # Source url
                    regular_expression = (r"/tracs/%s/browser(/(?:\w+/)?"
                                          r"%s[^?]*)(?:\?(.+))?" % (self.trac_env_name,
                                                                    path[0]))
                    source_url = util.url_from_browse(self.env,
                                                      util.unicode_unquote_plus(href),
                                                      regular_expression)

                    if 'BRANCH_VIEW' in req.perm and 'caller' in req.args and isinstance(req.args.get('caller'), str):
                        caller = req.args.get('caller')

                        if util.is_branch_name(caller):
                            branch_id = caller[1:]
                            branch = model.Branch(self.env, branch_id)

                            if branch.exists:
                                # 'Back to branch...'
                                stream |= Transformer('//div[@id="anydiff"]/form'
                                                      '/div/input[@type="submit"]').after(
                                    tag.div(tag.input(value_="Back to branch ...",
                                                      name_='branch_modification',
                                                      title_=('Back to branch %s' %
                                                              caller),
                                                      type_='button',
                                                      class_='buttons'),
                                            style_='margin-left:10px;'))
                                url = Href(self.env.base_url)
                                url_branch_modification = url.admin('tags_mgmt',
                                                                    'branches',
                                                                    branch_id,
                                                                    ci_source_url=source_url)
                                stream |= Transformer('//input[@name='
                                                      '"branch_modification"]').attr(
                                    'onclick', ('location.href="%s"' % url_branch_modification))

                    if 'BRANCH_CREATE' in req.perm and 'caller' not in req.args:
                        # 'Create branch...'
                        stream |= Transformer('//div[@id="anydiff"]/form'
                                              '/div/input[@type="submit"]').after(
                            tag.div(tag.input(value_="Create branch ...",
                                              name_='branch_creation',
                                              title_=("The branch will be created "
                                                      "on the selected directory and revision "
                                                      "(HEAD or older)"),
                                              type_='button',
                                              class_='buttons'),
                                    style_='margin-left:10px;'))
                        url = Href(self.env.base_url)
                        url_branch_creation = url.admin('tags_mgmt',
                                                        'branches',
                                                        ci_source_url=source_url)
                        stream |= Transformer('//input[@name='
                                              '"branch_creation"]').attr(
                            'onclick',
                            ('location.href="%s"' % url_branch_creation))

        return stream

    # IRequestFilter methods

    def pre_process_request(self, req, handler):
        """The pre-processing done when a request is submitted to TRAC """

        # We use this pre-processing for redirecting to a dedicated url that
        # forces authentication - originally authentication is not required by Trac
        # Once authentication is done, the browser will authenticate automatically
        # so redirection is required only for 'anonymous' user
        # All pre-processings are done for each request, whatever the target url,
        # so we choose this one as it seems more appropriate to relate
        # authentication to the admin sphere AND also to get authorization data
        # for users trying to authenticate.
        # We excluded from redirection 3 requests:
        #    match1: 1 request issued from page of all projects
        #    match2: 2 requests issued from ProtocolHandler
        # Those requests cannot be sent but anonymously
        if req.authname == "anonymous":
            match1 = re.match(r"/admin_xhrget", req.path_info)
            match2 = re.match(r"/xhrpost", req.path_info)
            if (not (match1 and req.method == "GET" and req.args.get('panel') == "users") and
                not (match2 and req.method == "POST" and req.args.get('action') in ("prepare_src", "commit")) and
                not (req.method == "GET" and req.path_info == '/admin/general/users' and req.args.get('action') == 'SOX')):
                # Force identification if not excluded from redirection
                url = req.abs_href(
                    "mylogin",
                    path_info=req.environ["PATH_INFO"],
                    query_string=req.environ["QUERY_STRING"],
                )
                req.redirect(url)

        if (req.path_info == '/admin/general/perm' and req.method == 'POST'):
            if 'add' in req.args and 'group' in req.args:
                req.session.set('add', 'True')
                req.session.set('subject', req.args['subject'])
            if 'remove' in req.args:
                req.session.set('remove', 'True')
                req.session.set('sel', repr(req.args['sel']))

        if ((req.path_info == '/admin/general/perm' and
             req.method == 'POST' and
             ('add' in req.args or
              'remove' in req.args)) or
            (req.path_info.startswith('/admin/tags_mgmt/branches') and
             req.method == 'POST' and
             ('apply' in req.args or
              'remove' in req.args))):
            req.session.set('authz_change', 'True')

        return handler

    def post_process_request(self, req, template, data, content_type):
        """The post-processing done when a request is submitted to TRAC
           This is used for patching data before template computing
           when processing is done by TRAC itself """

        if data is None:
            data = {}

        # Version tag ('caller') support:
        #   the browser is used for changing the url or the revision
        data['caller'] = req.args.get('caller', None) if isinstance(req.args.get('caller'), str) else None
        data['url_add_params'] = util.url_add_params

        # View documents/components
        if (template in ['documents.html',
                         'admin_components.html'] and
            'VERSION_TAG_VIEW' in req.perm):
            def get_panel_href(review=None):
                if review is None:
                    return partial(req.href, 'admin',
                                   VersionTagsAdminPanel.cat_type,
                                   VersionTagsAdminPanel.page_type)
                else:
                    return partial(req.href, 'admin',
                                   MilestoneTagsAdminPanel.cat_type,
                                   MilestoneTagsAdminPanel.page_type)

            data['get_panel_href'] = get_panel_href

            data['env'] = self.env
            if req.args.get('path_info'):
                # Detail view
                add_ctxtnav(req, _('View Associated Version Tags'),
                            href='%s/admin/tags_mgmt/version_tags?filter_value=%s]' %
                                 (req.base_path, req.args.get('path_info')))
                add_ctxtnav(req, _('View Associated Versions'),
                            href='%s/admin/tags_mgmt/versions?configuration_item=%s' %
                                 (req.base_path, req.args.get('path_info')))
            else:
                add_ctxtnav(req, _('View Version Tags'),
                            href='%s/admin/tags_mgmt/version_tags' %
                                 req.base_path)
                add_ctxtnav(req, _('View Versions'),
                            href='%s/admin/tags_mgmt/versions' %
                                 req.base_path)
            if template == 'documents.html':
                # 'Available PDF Packages'
                href = '/PDF-packaging/%s' % self.trac_env_name
                add_ctxtnav(req, _('Available PDF Packages'), href=href)

        # View versions
        if (template in ['admin_versions.html'] and
            'VERSION_TAG_VIEW' in req.perm):
            version = req.args.get('path_info')
            if version:
                # Detail view
                if version != 'Dummy':
                    try:
                        seen = set()
                        ci = [tg.tracked_item for tg in
                              model.Tag.select(self.env,
                                               ['tagged_item = "%s"' % version],
                                               tag_type='version_tags')
                              if tg.tracked_item not in seen and
                              not seen.add(tg.tracked_item)][0]
                    except Exception:
                        raise TracError(_('Missing version tag '
                                          'for version %s' % version))
                    try:
                        model.Document(self.env, ci)
                        is_document = True
                    except ResourceNotFound:
                        is_document = False
                    if is_document:
                        regular_expression = (r"\A" +
                                              self.program_name +
                                              r"_(%s)_" %
                                              self.env.config.get(
                                                  'ticket-custom',
                                                  'skill.options'))
                        match = re.search(regular_expression, version)
                        if match:
                            prf_report = self.env.config.get('artusplugin', 'PRF_report')
                            skill = match.group(1)
                            add_ctxtnav(req, _('View Associated (P)RFs'),
                                        href='%s/report/%s?SKILL=%s&VERSION=%s' %
                                             (req.base_path,
                                              prf_report,
                                              skill,
                                              version))
                    add_ctxtnav(req, _('View Associated Version Tags'),
                                href='%s/admin/tags_mgmt/version_tags?filter_value=%s' %
                                (req.base_path, version))
                    if not version.startswith('ECM_'):
                        if is_document:
                            doc_tktid = util.get_doc_tktid(self.env, version)
                            if doc_tktid:
                                add_ctxtnav(req, _('View Associated DOC ticket'),
                                            href=req.href('ticket', doc_tktid))
                            add_ctxtnav(req, _('View Associated Document'),
                                        href='%s/admin/tags_mgmt/documents/%s' %
                                        (req.base_path, ci))
                        else:
                            add_ctxtnav(req, _('View Associated Component'),
                                        href='%s/admin/tags_mgmt/components/%s' %
                                        (req.base_path, ci))

            else:
                if req.args.get('configuration_item'):
                    seen = set()
                    vers = [tg.tagged_item
                            for tg in model.Tag.select(
                                self.env,
                                ['tracked_item = "%s"' %
                                 req.args.get('configuration_item')],
                                tag_type='version_tags')
                            if tg.tagged_item not in seen and
                            not seen.add(tg.tagged_item)]
                    data['versions'] = [ver for ver in data['versions']
                                        if ver.name in vers]
                    add_ctxtnav(req, _('View Version Tags'),
                                href='%s/admin/tags_mgmt/version_tags?filter_value=%s' %
                                (req.base_path,
                                 req.args.get('configuration_item')))
                else:
                    add_ctxtnav(req, _('View Version Tags'),
                                href='%s/admin/tags_mgmt/version_tags' %
                                req.base_path)
                add_ctxtnav(req, _('View Components'),
                            href='%s/admin/tags_mgmt/components' %
                            req.base_path)
                add_ctxtnav(req, _('View Documents'),
                            href='%s/admin/tags_mgmt/documents' % req.base_path)
                data['versions'].sort(key=operator.attrgetter('name'))

        # View milestones
        if (template in ['admin_milestones.html'] and
            'MILESTONE_TAG_VIEW' in req.perm):
            milestone = req.args.get('path_info')
            if milestone:
                # Detail view
                if milestone != 'Dummy':
                    add_ctxtnav(req, _('View Associated ECRs'),
                                href=('%s/query?group=milestone&max=200'
                                      '&order=resolution&col=id&col=summary'
                                      '&col=status&col=owner&col=milestone'
                                      '&col=resolution&col=company&col=ecrtype'
                                      '&col=blocking&col=blockedby&col=keywords'
                                      '&col=time&type=ECR&milestone=%s') %
                                (req.base_path, milestone))
                    add_ctxtnav(req, _('View Associated Milestone Tags'),
                                href='%s/admin/tags_mgmt/milestone_tags?filter_value=%s' %
                                (req.base_path, milestone))
                    context = Context.from_request(req,
                                                   Milestone(self.env,
                                                             milestone).resource)
                    data['attachments'] = AttachmentModule(self.env).attachment_data(context)
                else:
                    data['attachments'] = None
            else:
                add_ctxtnav(req, _('View Milestone Tags'),
                            href='%s/admin/tags_mgmt/milestone_tags' %
                            req.base_path)

                def keyFunc(o):
                    """ returns a sort key for object o """
                    return o[0].name

                data['milestones'].sort(key=keyFunc)
                data['attachments'] = None

        # Manage Permissions and Groups
        if (template in ['admin_perms.html']):
            # Select Subject and Group through drop-downs
            users = util.Users(self.env)
            # Subject
            data['users'] = users.users_ldap_names
            # Group
            data['user_profiles'] = users.displayed_profiles.items()
            data['user_roles'] = users.displayed_roles.items()
            data['user_check'] = users.user_check
            data['role_check'] = users.role_check
            data['group_check'] = users.group_check

            # Set specified email if user added and external
            if (req.session.setdefault('add', 'False') == 'True'):
                subject = req.session['subject']
                with Ldap_Utilities() as ldap_util:
                    if ldap_util.user_is_external(subject):
                        mail = ldap_util.get_meggitt_mail(subject)
                        resolver = SpecifiedEmailResolver(self.env.compmgr)
                        resolver.set_address_for_name(mail, subject)
                req.session['add'] = 'False'

            # Unset specified email if user(s) removed
            if (req.session.setdefault('remove', 'False') == 'True'):
                sel = eval(req.session['sel'])
                sel = sel if isinstance(sel, list) else [sel]
                for key in sel:
                    subject = unicode_from_base64(key.split(':', 1)[0])
                    if (subject not in users.user_profiles and
                        subject not in users.project_users):
                        # A user has been removed
                        resolver = SpecifiedEmailResolver(self.env.compmgr)
                        resolver.remove_address_for_name(subject)

                req.session['remove'] = 'False'

            # Authz file update
            if (req.session.setdefault('authz_change', 'False') == 'True'):
                req.session['authz_change'] = 'False'

                # Force update of UsersPermissions component
                util.Users(self.env).__init__()

                # Get users associated with a profile
                # through roles or directly
                users_associated_with_profile = {}
                for profile in users.user_profiles:
                    users_associated_with_profile[profile] = sorted(set(
                        users.users_with_role_by_profile[profile] +
                        users.users_without_role_by_profile[profile]))

                flags = os.O_CREAT + os.O_WRONLY + os.O_EXCL
                if hasattr(os, 'O_BINARY'):
                    flags += os.O_BINARY

                # Write Trac GroupFile
                group_file = self.config.get('artusplugin', 'group_file')
                if not group_file:
                    raise TracError(_('group_file is not defined '
                                      'in project configuration file'))
                if os.access(group_file, os.F_OK):
                    os.remove(group_file)
                targetfile = os.fdopen(os.open(group_file, flags, 666), 'w')
                for profile in users.user_profiles:
                    targetfile.write("%s: %s\n" %
                                     (profile,
                                      ' '.join(users_associated_with_profile[profile])))
                targetfile.close()

                # SVN AuthzFile is rebuilt from a template in local templates
                svn_authz_template_file_path = "%s/%s" % (
                    Chrome(self.env).get_templates_dirs()[0],
                    self.config.get('artusplugin', 'svn_authz_template_file'))

                user_profiles = users.user_profiles

                class MyConfigDict(ConfigDict):
                    """ """

                    def __init__(self, *args, **kwargs):
                        super(MyConfigDict, self).__init__(*args, **kwargs)

                    def mysort(self, key):
                        if key in user_profiles:
                            return '%d' % user_profiles.index(key)
                        else:
                            match = re.search('\A(/branches/B)(\d+)\Z', key)
                            if match:
                                return '%s%03d' % (match.group(1), int(match.group(2)))
                            else:
                                return key

                authz_config = ConfigParser.RawConfigParser(dict_type=MyConfigDict)
                authz_config.optionxform = str
                authz_config.read(svn_authz_template_file_path)

                # Add special users for SVN access
                if 'admin' in users.user_profiles:
                    users_associated_with_profile['admin'].insert(0, 'trac')
                if 'authorized' in users.user_profiles:
                    users_associated_with_profile['authorized'].insert(0, 'buildbot')
                    users_associated_with_profile['authorized'].insert(0, 'trac')
                if 'developer' in users.user_profiles:
                    users_associated_with_profile['developer'].insert(0, 'trac')
                if 'authenticated' in users.user_profiles:
                    users_associated_with_profile['authenticated'].insert(0, 'trac')

                # Set SVN AuthzFile users for each TRAC profile
                section = 'groups'
                if not authz_config.has_section(section):
                    authz_config.add_section(section)
                for profile in users.user_profiles:
                    authz_config.set(section,
                                     profile,
                                     ','.join(users_associated_with_profile[profile]))

                # Set SVN AuthzFile branches restrictions
                conf_mgmt = self.env.config.get('artusplugin', 'conf_mgmt', '-1')
                if conf_mgmt == '1':
                    section = ':glob:/branches/B*'
                    authz_config.add_section(section)
                    authz_config.set(section, '@authenticated', 'r')
                    for group in ('@developer', '@authorized', '@admin'):
                        authz_config.set(section, group, 'rw')

                # Write SVN AuthzFile
                authz_file = self.config.get('trac', 'authz_file')
                if os.access(authz_file, os.F_OK):
                    os.remove(authz_file)
                targetfile = os.fdopen(os.open(authz_file, flags, 666), 'w')
                authz_config.write(targetfile)
                targetfile.close()
                for line in fileinput.input(authz_file, inplace=1):
                    print(line.replace(' = ', '='), end='')

        return (template, data, content_type)

    # IRequestHandler methods

    def match_request(self, req):
        """ Customization of some requests handling """
        match = re.match(r"/mylogin", req.path_info)
        if match and req.method == "GET":
            return True
        match = re.match(r'/admin_xhrget', req.path_info)
        if match:
            req.args = util.parse_query_string(req.query_string)
            return True

    def process_request(self, req):
        """ Customization of some requests handling """

        if req.path_info.startswith("/mylogin"):
            login_module = LoginModule(self.env)
            login_module._do_login(req)
            # original 'path_info' and 'query_string' are passed through the env QUERY_STRING parameter
            url = req.abs_href()
            query_string = unquote(req.environ["QUERY_STRING"]).decode('utf-8')
            original_path_info = query_string[query_string.find("&path_info=") :][
                len("&path_info=") :
            ]
            url += original_path_info
            original_query_string = query_string[: query_string.find("&path_info=")][
                len("query_string=") :
            ]
            # Cleaning
            args = parse_arg_list(original_query_string)
            args = ["%s=%s" % (arg[0], arg[1]) for arg in args if arg[0] != "login"]
            # Build url and redirect
            if args:
                url += "?" + "&".join(args)
            req.redirect(url)

        elif req.path_info.startswith('/admin_xhrget'):
            panel = req.args.get('panel')
            if panel == "users":
                users = util.Users(self.env)
                user = req.args.get("user")
                response = {}
                # Check if user is registered with Trac
                response['registered'] = user in users.registered_users
                # Check if user has access to the project
                response['access'] = user in users.project_users
                return_value = json.dumps(response)
                req.send(return_value.encode("utf-8"))
            else:
                key = req.args.get('key')
                field_name = req.args.get('field_name')
                attribute_name = req.args.get('attribute_name')
                if panel in ('version_tags', 'milestone_tags', 'documents'):
                    version_tag = self.get_tag(key)
                    if field_name == 'version_tag' and attribute_name == 'title':
                        attribute_value = self.get_vtag_description(version_tag)
                        req.send(attribute_value.encode("utf-8"))
                    elif field_name == 'revision' and attribute_name == 'title':
                        tag_url = version_tag.tag_url
                        revision = util.get_revision(tag_url)
                        changeset = util.get_repository(self.env, tag_url).get_changeset(revision)
                        attribute_value = ('Changeset date: %s' %
                                           self.get_formatted_datetime(req, changeset.date))
                        req.send(attribute_value.encode("utf-8"))
                    elif field_name == 'ecm_tktid' and attribute_name == 'title':
                        ecm_tktid = util.get_ecm_tktid(
                            self.env, version_tag.tagged_item)
                        ecm_tkt = Ticket(self.env, ecm_tktid)
                        attribute_value = ('%s (owner: %s - status: %s - resolution: %s)' %
                                           (ecm_tkt['summary'],
                                            ecm_tkt['owner'],
                                            ecm_tkt['status'],
                                            'N/A' if not ecm_tkt['resolution'] else ecm_tkt['resolution']))
                        req.send(attribute_value.encode("utf-8"))
                    elif field_name == 'fee_tktid' and attribute_name == 'title':
                        fee_tktid = util.get_fee_tktid(
                            self.env, version_tag.tagged_item)
                        fee_tkt = Ticket(self.env, fee_tktid)
                        attribute_value = ('%s (owner: %s - status: %s - resolution: %s)' %
                                           (fee_tkt['summary'],
                                            fee_tkt['owner'],
                                            fee_tkt['status'],
                                            'N/A' if not fee_tkt['resolution'] else fee_tkt['resolution']))
                        req.send(attribute_value.encode("utf-8"))
                    elif field_name == 'doc_tktid' and attribute_name == 'title':
                        doc_tktid = util.get_doc_tktid(
                            self.env, version_tag.tagged_item)
                        doc_tkt = Ticket(self.env, doc_tktid)
                        attribute_value = ('%s (owner: %s - status: %s - resolution: %s)' %
                                           (doc_tkt['summary'],
                                            doc_tkt['owner'],
                                            doc_tkt['status'],
                                            'N/A' if not doc_tkt['resolution'] else doc_tkt['resolution']))
                        req.send(attribute_value.encode("utf-8"))
                    elif field_name == 'mom_tktid' and attribute_name == 'title':
                        mom_tktid = util.get_mom_tktid(
                            self.env, version_tag)
                        mom_tkt = Ticket(self.env, mom_tktid)
                        attribute_value = ('%s (owner: %s - status: %s - resolution: %s)' %
                                           (mom_tkt['summary'],
                                            mom_tkt['owner'],
                                            mom_tkt['status'],
                                            'N/A' if not mom_tkt['resolution'] else mom_tkt['resolution']))
                        req.send(attribute_value.encode("utf-8"))

    # ITimelineEventProvider methods

    def get_timeline_filters(self, req):
        if 'VERSION_TAG_DELETE' in req.perm:
            yield ('document', _('Documents changes'))

    def get_timeline_events(self, req, start, stop, filters):
        ticket_types = [tkt_type.name for tkt_type in Type.select(self.env)]
        if 'DOC' in ticket_types and 'document' in filters:
            ts_start = to_utimestamp(start)
            ts_stop = to_utimestamp(stop)
            with self.env.db_query as db:
                for name, t, author, field, oldvalue, newvalue \
                        in db("""
                        SELECT d.name, dc.time, dc.author,
                               dc.field, dc.oldvalue, dc.newvalue
                        FROM document_change dc
                            INNER JOIN document d ON d.name = dc.document
                                AND dc.time>=%s AND dc.time<=%s
                        ORDER BY dc.time
                        """ % (ts_start, ts_stop)):
                    document = model.Document(self.env, name)
                    if ((oldvalue, newvalue) not in
                        [(None, '1'),
                         ('1', None),
                         (None, ""),
                         ("", None)]):
                        yield ('document', from_utimestamp(t), author,
                               (document, field, oldvalue, newvalue))

    def render_timeline_event(self, context, field, event):
        document, prop, oldvalue, newvalue = event[3]
        if field == 'url':
            return context.href.admin('tags_mgmt', 'documents', document['name'])
        elif field == 'title':
            return tag_('Document %(name)s changed',
                        name=tag.em(document['name']))
        elif field == 'description':
            labels = {
                'name': "Name",
                'shortname': "Short Name",
                'description': "Description",
                'builder': "Builder Document",
                'source': "Source Document",
                'sourcetype': "Source Type",
                'pdfsigned': "PDF digitally signed",
                'controlcategory': "Control Category",
                'independence': "Verified with independence",
                'submittedfor': "Submitted for"
            }
            values = {
                'controlcategory': {None: "N/A",
                                    '0': "N/A",
                                    '1': "CC1/HC1",
                                    '2': "CC2/HC2"},
                'independence': {None: "Checked",
                                 '0': "Unchecked",
                                 '1': "Checked"},
                'submittedfor': {None: "Approval",
                                 '0': "Approval",
                                 '1': "Review",
                                 '2': "Information"}
            }
            oldv = (values[prop][oldvalue] if
                    prop in values and oldvalue in values[prop] else
                    oldvalue)
            newv = (values[prop][newvalue] if
                    prop in values and newvalue in values[prop] else
                    newvalue)
            descr = '%s changed from "%s" to "%s"' % (
                labels[prop],
                oldv,
                newv)
            return descr

    # The following 5 functions are to be moved at the appropriate place

    def get_tag(self, tag_name):
        if tag_name:
            v = model.Tag(self.env, name=tag_name)
        else:
            v = None
        return v

    # Node data for tag rev date display through title
    def get_formatted_datetime(self, req, date):
        display_date = format_datetime(date, 'iso8601', req.tz)
        fmt = req.session.get('datefmt')
        if fmt and fmt != 'iso8601':
            display_date = format_datetime(date, fmt, req.tz)
        return display_date

    # Get log message
    def get_log_message(self, tag_url):
        """
        tag_url : [reponame]/[trunk|branches|tags]/.../tag_name?rev=...
        """
        changeset = util.get_repository(self.env,
                                        tag_url).get_changeset(util.get_revision(tag_url))
        return changeset.message.replace('\n', ' ')

    # Version description
    def get_version_description(self, vtag_name):
        tagged_item = self.get_tag(vtag_name).tagged_item
        try:
            w = Version(self.env, name=tagged_item)
            description = w.description
        except ResourceNotFound:
            description = ''
        return description

    # Version tag description
    def get_vtag_description(self, vtag):
        description = self.get_version_description(vtag.name)
        if vtag.tag_url:
            if description:
                description += ' - '
            description += self.get_log_message(vtag.tag_url)
        return description


class ReqtifyProject(object):
    """ Gives services around a Reqtify project """

    def __init__(self, env, fileobj, filepath):
        """ Initializations """
        self.env = env
        self.filepath = filepath
        self.filename = os.path.basename(filepath)
        self.dirname = os.path.dirname(self.filepath)

        if not os.access(self.dirname, os.F_OK):
            os.mkdir(self.dirname)

        if os.access(self.filepath, os.F_OK):
            os.remove(self.filepath)

        self.flags = os.O_CREAT + os.O_WRONLY + os.O_EXCL
        if hasattr(os, 'O_BINARY'):
            self.flags += os.O_BINARY

        targetfile = os.fdopen(os.open(self.filepath, self.flags, 666), 'wbaseline_docs')
        shutil.copyfileobj(fileobj, targetfile)
        targetfile.close()

        config = ConfigParser.RawConfigParser()
        config.optionxform = str
        config.read(self.filepath)
        self.config = config
        self.sections = self.config.sections()

    def _docname_from_path(self, path, document_pattern):
        """ Extracts normalized document name from given path """
        segments = path.replace('\\', '/').split('/')
        seg_len = len(segments)
        if '.' in segments[-1]:
            seg_idx = -2
        else:
            seg_idx = -1
        docname = None
        while seg_idx >= 0 - seg_len:
            docname = segments[seg_idx]
            seg_idx -= 1
            match = re.search(document_pattern, docname)
            if match:
                docname = match.group(1)
                break
        return docname

    def read(self, document_pattern):
        """ Extracts documents names and paths from Reqtify project file """
        docs = {}
        for section in self.sections:
            if self.config.has_option(section, 'Type'):
                options = ['Path']
                rexp = r'\AModificationDocument\d+\Z'
                options += [opt for opt in self.config.options(section) if
                            re.search(rexp, opt)]
                for option in options:
                    path = self.config.get(section, option)
                    docname = self._docname_from_path(path, document_pattern)
                    if docname:
                        if docname in docs:
                            docs[docname].append((section, option, path))
                        else:
                            docs[docname] = [(section, option, path)]
        return docs

    def update(self, p_docs, b_docs, rexp_t, tagname):
        """ Return an  updated project definition
            p_docs : Reqtify project documents (input)
            b_docs : Baseline documents
            rexp_t : regular expression template for checking reqtify project path syntax
            tagname : baseline used to update the Reqtify project
        """

        p_docs_updated = p_docs.copy()
        for tracked_item in p_docs.iterkeys():
            rexp = rexp_t % (tracked_item, tracked_item)
            options = []
            for opttuple in p_docs[tracked_item]:
                oldpath = opttuple[2]
                match = re.search(rexp, oldpath)
                if match:
                    endpath = util.path_to_windows(match.group(2))
                    if tracked_item in b_docs:
                        newpath = b_docs[tracked_item][0]
                    elif (oldpath.startswith('trunk/') or
                          oldpath.startswith('branches/')):
                        newpath = tracked_item
                    elif oldpath.startswith('tags/'):
                        newpath = match.group(1)
                    else:
                        raise TracError(tag.p("Document %s is local and not included in the baseline %s" % (tracked_item, tagname), class_="message"))
                else:
                    raise TracError(tag.p("Option %s of section %s has not the expected format" % (opttuple[1], opttuple[0]), class_="message"))
                if endpath:
                    newpath = '%s\%s' % (newpath, endpath)
                options.append((opttuple[0], opttuple[1], newpath))
            p_docs_updated[tracked_item] = options
        return p_docs_updated

    def write(self, docs):
        """ Writes back Reqtify project file """
        for docname in docs.iterkeys():
            for opttuple in docs[docname]:
                self.config.set(opttuple[0], opttuple[1], opttuple[2])

        for section in self.sections:
            if self.config.has_option(section, 'Type'):
                options = ['RemoteConfig', 'IntermediateAccessFilename', 'AbsolutePath']
                rexp1 = r'\AModificationDocument\d+RemoteConfig\Z'
                rexp2 = r'\AModificationDocument\d+IntermediateAccessFilename\Z'
                rexp3 = r'\AModificationDocument\d+AbsolutePath\Z'
                options += [opt for opt in self.config.options(section) if
                            re.search(rexp1, opt) or
                            re.search(rexp2, opt) or
                            re.search(rexp3, opt)]
                for option in options:
                    self.config.remove_option(section, option)
                rexp4 = r'\AVariable\d+Name\Z'
                rexp5 = r'\AModificationDocument\d+Variable\d+Name\Z'
                options = [opt for opt in self.config.options(section) if
                           re.search(rexp4, opt) or
                           re.search(rexp5, opt)]
                for option in options:
                    if self.config.get(section, option) == 'server':
                        self.config.remove_option(section, option)
                        self.config.remove_option(section, option.replace('Name', 'Value'))

        if os.access(self.filepath, os.F_OK):
            os.remove(self.filepath)

        targetfile = os.fdopen(os.open(self.filepath, self.flags, 666), 'w')
        self.config.write(targetfile)
        targetfile.close()

        for line in fileinput.input(self.filepath, inplace=1):
            print(line.replace(' = ', '='), end='')

    def export(self, baseline, baseline_url, baseline_items, p_docs, batchfile):
        export_cmds = []
        export_tmpl = 'svn export --force "%(URL)s" "%(PATH)s"\n'
        for doc in p_docs.iterkeys():
            old_path = p_docs[doc][-1][2]
            seg_list = old_path.replace('\\', '/').rsplit('/', 1)
            if '.' in seg_list[-1]:
                doc_file = seg_list[-1]
            else:
                doc_file = None
            if doc in baseline_items:
                subpath = baseline_items[doc][1]
                exported_dir = baseline_items[doc][0]
                if baseline_url:
                    url = util.get_url(util.get_repo_url(self.env, baseline_url))
                    url = '%s/%s/%s' % (url, util.path_to_linux(subpath), exported_dir)
                else:
                    url = util.get_repo_url(self.env, baseline_items[doc][2])
                if doc_file:
                    url = '%s/%s' % (url, doc_file)
            else:
                # url cannot be local at this stage
                url = util.get_repo_url(self.env, '/%s' % old_path)
                seg_list = url.rsplit('/', 2)
                exported_dir = seg_list[2]
                if doc_file:
                    exported_dir = seg_list[1]
            new_path = '%%CD%%\\%s' % exported_dir
            if doc_file:
                new_path = '%s\\%s' % (new_path, doc_file)
            if doc_file:
                export_cmds.append('mkdir %s\n' % exported_dir)
            export_cmds.append(export_tmpl % {'URL': url, 'PATH': new_path})
        if os.access(batchfile, os.F_OK):
            os.remove(batchfile)
        f = open(batchfile, "w")
        try:
            f.writelines(export_cmds)
        finally:
            f.close()


class ServerMgmt(CComponent):
    """ Admin panel for Server Management. """

    implements(IAdminPanelProvider, IPermissionRequestor)

    abstract = True

    cat_type = 'server_mgmt'
    _cat_label = 'Server Mgmt'

    # IAdminPanelProvider

    def get_admin_panels(self, req):
        if 'APACHE_RESTART' in req.perm:
            yield (self.cat_type, self._cat_label, self.page_type, self.page_label[1])

    def render_admin_panel(self, req, cat, page, path_info):
        # Trap AssertionErrors and convert them to TracErrors
        try:
            my_script_list = glob.glob('%s/../htdocs/stamped/admin_*' % os.path.dirname(os.path.realpath(__file__)))
            if len(my_script_list) != 1:
                raise TracError(_("More than one admin.js script or none."))
            else:
                add_script(req, 'artusplugin/stamped/%s' % os.path.basename(my_script_list[0]))
            add_script(req, 'common/js/wikitoolbar.js')
            return self._render_admin_panel(req, cat, page)
        except AssertionError as e:
            raise TracError(e)


class ApacheMgmtAdminPanel(ServerMgmt):
    """ Admin panel for Apache Server Management. """

    page_type = 'apache_mgmt'
    page_label = ('Apache Mgmt', 'Apache Mgmt')

    # IPermissionRequestor method

    def get_permission_actions(self):
        """ Definition of rights regarding Apache management
        APACHE_RESTART : right to restart the Apache file server

                                authenticated      developer     authorized      admin
        APACHE_RESTART                                                             X

        """
        return ['APACHE_RESTART']

    # ApacheMgmtAdminPanel methods

    def _render_admin_panel(self, req, cat, page):

        if req.method == 'POST':
            # apache_user = subprocess.check_output("grep -Po '\AUser\s+\K.+' /etc/httpd/conf/httpd.conf")
            apache_user = 'apache'
            apache_homedir = os.path.expanduser('~%s' % apache_user)
            if 'apache_graceful' in req.args or 'apache_forceful' in req.args:
                # Restart Apache server
                param = 'graceful' if 'apache_graceful' in req.args else 'restart'
                logfile_path = '/var/cache/trac/apache/restartHttpd.log'
                unix_cmd = '/srv/trac/common/restartHttpd.sh %s %s' % (param, logfile_path)
                subprocess.Popen(unix_cmd.encode('utf-8'),
                                 shell=True,
                                 cwd=apache_homedir,
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT,
                                 env={'LC_ALL': 'fr_FR.utf8',
                                      'HOME': apache_homedir,
                                      'PYTHONIOENCODING': 'utf-8'})
                mode = 'graceful' if param == 'apache_graceful' else 'forceful'
                message = tag.p(_("The Apache server "), tag.em(_(mode)), _(" restart has been launched."), class_="message")
                add_notice(req, message)
            elif 'wsgi_graceful' in req.args or 'wsgi_forceful' in req.args:
                # Restart WSGI application
                # Cf: https://code.google.com/archive/p/modwsgi/wikis/ReloadingSourceCode.wiki
                # Cf: https://httpd.apache.org/docs/2.4/stopping.html
                sig = signal.SIGUSR1 if 'wsgi_graceful' in req.args else signal.SIGTERM
                os.kill(os.getpid(), sig)
                mode = 'graceful' if sig == signal.SIGUSR1 else 'forceful'
                message = tag.p(_("The WSGI application "), tag.em(_(mode)), _(" restart has been launched."), class_="message")
                add_notice(req, message)

        data = {}
        return 'apache_mgmt.html', data


class TagsMgmt(CComponent):
    """ Admin panels for Tags Management. """

    implements(IAdminPanelProvider, IPermissionRequestor)

    abstract = True

    cat_type = 'tags_mgmt'
    _cat_label = 'Conf Mgmt'

    # IAdminPanelProvider

    def get_admin_panels(self, req):
        if 'VERSION_TAG_VIEW' in req.perm or 'MILESTONE_TAG_VIEW' in req.perm:
            yield (self.cat_type, self._cat_label, self.page_type, self.page_label[1])

    def render_admin_panel(self, req, cat, page, path_info):
        # Trap AssertionErrors and convert them to TracErrors
        try:
            my_script_list = glob.glob('%s/../htdocs/stamped/admin_*' % os.path.dirname(os.path.realpath(__file__)))
            if len(my_script_list) != 1:
                raise TracError(_("More than one admin.js script or none."))
            else:
                add_script(req, 'artusplugin/stamped/%s' % os.path.basename(my_script_list[0]))
            Chrome(self.env).add_wiki_toolbars(req)
            return self._render_admin_panel(req, cat, page, path_info)
        except AssertionError as e:
            raise TracError(e)


class BranchesAdminPanel(CComponent):
    """ Admin panels for Branches Management. """

    implements(IAdminPanelProvider, IPermissionRequestor)

    cat_type = 'tags_mgmt'
    _cat_label = 'Conf Mgmt'

    page_type = 'branches'
    page_label = ('Branch', 'Branches')

    _actions = ['BRANCH_VIEW', 'BRANCH_CREATE', 'BRANCH_MODIFY', 'BRANCH_APPLY', 'BRANCH_DELETE']
    _admin_privilege = 'BRANCH_ADMIN'

    # IPermissionRequestor method

    def get_permission_actions(self):
        """ Definition of rights regarding branches management
        BRANCH_xxx : rights to view, create, modify, apply or delete a branch

        E.g.:
                                authenticated      developer     authorized      admin
        BRANCH_VIEW                   X               X              X            X
        BRANCH_CREATE                                                x            X
        BRANCH_MODIFY                                                X            X
        BRANCH_APPLY                                                 X            X
        BRANCH_DELETE                                                X(*)         X(**)
        BRANCH_ADMIN                                                              X

        (*) Branch can be deleted by authorized user only if not yet applied ('applied' means branch created in the repository)
        (**) Branch can be deleted by admin user only if no version tag is applied on it

        """
        return self._actions + [(self._admin_privilege, self._actions)]

    # IAdminPanelProvider methods

    def get_admin_panels(self, req):
        if 'BRANCH_VIEW' in req.perm:
            yield (self.cat_type, self._cat_label, self.page_type, self.page_label[1])

    def render_admin_panel(self, req, cat, page, branch):
        my_script_list = glob.glob('%s/../htdocs/stamped/admin_*' % os.path.dirname(os.path.realpath(__file__)))
        if len(my_script_list) != 1:
            raise TracError(_("More than one admin.js script or none."))
        else:
            add_script(req, 'artusplugin/stamped/%s' % os.path.basename(my_script_list[0]))
        Chrome(self.env).add_wiki_toolbars(req)

        db = self.env.get_db_cnx()
        data = init_data(self, req)
        data['get_last_path_rev_author'] = util.get_last_path_rev_author

        # Detail view?
        if branch:
            data['view'] = 'detail'
            br = model.Branch(self.env, branch, db=db)
            data['branch'] = br
            if br.source_url or br.source_tag:
                if br.source_url:
                    branch_source = br.source_url
                else:
                    branch_source = data['get_tag'](br.source_tag).tag_url
                repos = util.get_repository(self.env, branch_source)
                source_dir = data['get_url'](branch_source).split('/')[-1]
                reponame = repos.reponame
                branch_url = '/branches/B%s' % br.id
                if reponame:
                    branch_url = '/%s%s' % (reponame, branch_url)
                data['branch_url'] = branch_url
            else:
                source_dir = None
                data['branch_url'] = None

            # Selected source_tag / source_url
            if req.args.get('ci_source_url'):
                data['source_tag'] = ''
                data['source_url'] = req.args.get('ci_source_url')
            else:
                if 'source_tag' in req.args:
                    data['source_tag'] = req.args.get('source_tag')
                    data['source_url'] = ''
                else:
                    if 'source_url' in req.args:
                        data['source_url'] = req.args.get('source_url')
                        data['source_tag'] = ''
                    else:
                        if br.source_tag:
                            data['source_tag'] = br.source_tag
                        else:
                            data['source_tag'] = ''
                        if br.source_url:
                            data['source_url'] = br.source_url
                        else:
                            data['source_url'] = ''

            # Lists of all tags
            data['milestones'] = [tg.name for tg in model.Tag.select(self.env, ['tag_url IS NOT NULL'], db=db, tag_type='milestone_tags')]
            data['components'] = [tg.name for tg in model.Tag.select(self.env, ['tag_url IS NOT NULL', 'baselined = 1'], db=db, tag_type='version_tags')]
            data['documents'] = [tg.name for tg in model.Tag.select(self.env, ['tag_url IS NOT NULL', 'component = 0', "name not like 'ECM_%'"], db=db, tag_type='version_tags')]

            if req.method == 'POST':
                if req.args.get('change') == 'Apply changes':
                    # Save source tag or source url
                    if req.args.get('branch_source') == 'source_tag':
                        br.source_tag = req.args.get('source_tag')
                        br.source_url = None
                        br.update(db=db)
                    elif req.args.get('branch_source') == 'source_url':
                        br.source_url = req.args.get('source_url')
                        br.source_tag = None
                        br.update(db=db)
                    db.commit()
                    req.redirect(req.href.admin(cat, page, branch, ScrollX=req.args.get('ScrollX'), ScrollY=req.args.get('ScrollY')))

                elif req.args.get('save'):
                    # Save description
                    br.description = req.args.get('description')
                    br.update(db=db)
                    db.commit()
                    req.redirect(req.href.admin(cat, page, branch, ScrollX=req.args.get('ScrollX'), ScrollY=req.args.get('ScrollY')))

                # Create branch in the repository
                elif req.args.get('apply'):
                    if br.source_tag:
                        # Get Version Tags recursively
                        baselined_tags = []
                        nonbaselined_tags = []
                        self._baseline_items(br.source_tag, baselined_tags, nonbaselined_tags, db)
                        # Filter out tags without sources in the repository (EOC)
                        nonbaselined_tags = [vt for vt in nonbaselined_tags
                                             if '/trunk/' in vt.source_url or
                                             '/branches/' in vt.source_url]
                        # Keeps shallow version only for each tracked item
                        # according to the supposed will of the baseline's author
                        tracked_items = set()
                        shallow_tags = [vt for vt in nonbaselined_tags
                                        if vt.tracked_item not in tracked_items and
                                        not tracked_items.add(vt.tracked_item)]
                    else:
                        # br.source_url is just what we want in the following lines
                        shallow_tags = [br]

                    # List of source urls (without revisions)
                    source_urls = [util.get_url(vt.source_url) for vt in shallow_tags]
                    # List of source revisions
                    source_revisions = [util.get_revision(vt.source_url) for vt in shallow_tags]
                    # List of target urls
                    target_urls = self._chroot(source_urls, '/branches/B%s' % branch)
                    # Get list of target dirs to create recursively
                    target_dirs = []
                    if reponame:
                        exclude_dirs = ['/', '/%s' % reponame, '/%s/branches' % reponame]
                    else:
                        exclude_dirs = ['/', '/branches']
                    self._makedirs(self._splitpaths(target_urls), target_dirs, exclude_dirs)
                    target_dirs.sort()
                    # Create branch
                    if req.args.get('comment'):
                        comment = req.args.get('comment')
                    else:
                        if br.source_tag:
                            comment = _("Branch B%(branch)s created from source tag %(tag)s", branch=branch, tag=br.source_tag)
                        else:
                            comment = _("Branch B%(branch)s created from source url %(url)s", branch=branch, url=br.source_url)
                    description = comment
                    comment += _(" (on behalf of %(user)s)", user=req.authname)
                    msg_filename = self._logmessage(comment)
                    unix_cmd = util.SVNMUCC_TEMPLATE_CMD + '-F "%s" ' % msg_filename + '-U "%s" ' % self._rooturl(branch_source)
                    if not repos.has_node('/branches', ''):
                        if reponame:
                            unix_cmd += 'mkdir "/%s/branches" ' % reponame
                        else:
                            unix_cmd += 'mkdir "/branches" '
                    for target_dir in target_dirs:
                        unix_cmd += 'mkdir "%s" ' % target_dir
                    for rev, src_url, tgt_url in zip(source_revisions, source_urls, target_urls):
                        unix_cmd += 'cp %(rev)s "%(src_url)s" "%(tgt_url)s" ' % {'rev': rev, 'src_url': src_url, 'tgt_url': tgt_url}
                    unix_cmd_list = [unix_cmd]
                    retcode, lines = util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())
                    # Commit message file is removed
                    try:
                        os.remove(msg_filename)
                    except os.error:
                        pass
                    # Result of UNIX commands
                    if retcode != 0:
                        message = tag.p("Creation of ", tag.em("branch "), tag.a("B%s" % branch, href=req.href.admin(cat, page, branch)), " has failed.", class_="message")
                        for line in lines:
                            message(tag.p(line))
                        raise TracError(message)
                    else:
                        match = re.match(r'^r(\d+) committed by trac at', lines[0])
                        if not match:
                            branch_revision = ''
                            for line in lines:
                                if line.startswith(u'Révision '):
                                    regular_expression = u'\ARévision (\d+) propagée\.\n\Z'
                                    match = re.search(regular_expression, line)
                                    if match:
                                        branch_revision = match.group(1)
                                    break
                            if branch_revision == '':
                                message = tag.p("Creation of ", tag.em("branch "), tag.a("B%s" % branch, href=req.href.admin(cat, page, branch)), " has failed.", class_="message")
                                message(tag.p("Could not find revision associated with successful commit"))
                                raise TracError(message)
                        else:
                            branch_revision = match.group(1)
                        br.branch_url = branch_url + '?rev=' + branch_revision
                        br.author = req.authname
                        if not br.description:
                            br.description = description
                        br.update(db=db)
                        db.commit()

                    add_notice(req, tag(_('The branch '),
                                        tag.a('B%s' % branch, href="%s" % req.href.admin(cat, page, branch)),
                                        _(' has been created in the repository at revision '),
                                        tag.a('%s' % util.get_revision(br.branch_url),
                                              href="%s" % util.get_tracbrowserurl(self.env, util.get_url(br.branch_url))),
                                        '.'))
                    req.redirect(req.href.admin(cat, page, selected_item='B%s' % br.id, filter_value=source_dir))

                # Cancel
                elif req.args.get('cancel'):
                    req.redirect(req.href.admin(cat, page, selected_item='B%s' % br.id, filter_value=source_dir))

        else:
            if req.method == "POST":
                # Create Branch in the database
                if req.args.get('add'):
                    b = model.Branch(self.env, db=db)
                    # b.id will be auto-incremented when inserted
                    b.author = req.authname
                    if req.args.get('ci_source_url'):
                        b.source_url = req.args.get('ci_source_url')
                    else:
                        b.source_url = None
                    if req.args.get('ci_source_tag'):
                        b.source_tag = req.args.get('ci_source_tag')
                    else:
                        b.source_tag = None
                    # b.branch_url will be set when applied into the repository
                    b.insert(db=db)
                    db.commit()
                    b.refresh(self.env, db=db)
                    req.redirect(req.href.admin(cat, page, b.id))

                # Remove Branch from the database (and the repository if applied and feasible, ie no version tag on it)
                elif req.args.get('remove'):
                    sel = req.args.get('sel')
                    if not sel:
                        raise TracError(_('No branch selected'))
                    if not isinstance(sel, list):
                        sel = [sel]

                    # The branches can only be removed if no version tag(s) is applied on it
                    blocking = []
                    branch_segregation_activated = True if self.env.config.get('artusplugin', 'branch_segregation_activated') == 'True' else False
                    where_expr_list = ['tag_url IS NOT NULL', 'review IS NULL']
                    if not branch_segregation_activated:
                        where_expr_list.append('NOT baselined')
                    for bid in sel:
                        blocking += [(v.name, '%s' % bid) for v in model.Tag.select(self.env, where_expr_list + ['source_url GLOB "*/branches/B%s/*"' % bid], db=db)]
                    if blocking:
                        message = tag.p("Remove the listed version tag(s) in order to remove the associated branch(es).", class_="message")
                        for block in blocking:
                            message(tag.p("Can't remove ", tag.em("branch "), tag.a("B%s" % block[1], href=req.href.admin(cat, page, block[1])), " because version tag ", tag.a("%s" % block[0], href=data['get_panel_href'](data['get_tag'](block[0]).review)(block[0])), " has been applied on it."))
                        raise TracError(message, "Cannot delete branch(es) where version tag(s) are applied")

                    # Effective removal of selected branches
                    for bid in sel:
                        # If the branch being removed is tagged in the repository, it is also removed from the HEAD revision
                        b = model.Branch(self.env, bid, db=db)
                        if b.branch_url:
                            unix_cmd_list = [util.SVN_TEMPLATE_CMD % {'subcommand': 'delete -m "%s" "%s"' % (
                                _('Removal of branch B%(bid)s (on behalf of %(user)s)', bid=bid, user=req.authname),
                                util.get_url(util.get_repo_url(self.env, b.branch_url)))}]
                            retcode, lines = util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())
                            if retcode != 0:
                                message = tag.p("Removal of ", tag.em("branch(es)"), " in the repository has failed.", class_="message")
                                for line in lines:
                                    message(tag.p(line))
                                raise TracError(message)
                        b.delete(db=db)
                        # Effective update of the database
                        db.commit()

                    add_notice(req, _("The selected branches have been removed."))
                    req.redirect(req.href.admin(cat, page, filter_value=data['filter_value'], ScrollX=req.args.get('ScrollX'), ScrollY=req.args.get('ScrollY'), selected_item=req.args.get('selected_item')))

                # Source Url / Source Tag filter
                elif req.args.get('update'):
                    req.redirect(req.href.admin(cat, page, filter_value=data['filter_value'], ScrollX=req.args.get('ScrollX'), ScrollY=req.args.get('ScrollY'), selected_item=req.args.get('selected_item')))

            if req.args.get('ci_source_tag'):
                data['ci_source_tag'] = req.args.get('ci_source_tag')
            if req.args.get('ci_source_url'):
                data['ci_source_url'] = req.args.get('ci_source_url')

            # List all Branches
            if data['filter_value']:
                match = re.search(r"\AB(\d+)\Z", data['filter_value'])
                if match:
                    branches = [model.Branch(self.env, int(match.group(1)), db=db)]
                    branches += [branch for branch in model.Branch.select(self.env, ['source_url LIKE "%/' + data['filter_value'] + '?%"'], db=db)]
                else:
                    branches = [branch for branch in model.Branch.select(self.env, ['source_url LIKE "%' + data['filter_value'] + '%"'], db=db)]
                    branches += [branch for branch in model.Branch.select(self.env, ['source_tag LIKE "%' + data['filter_value'] + '%"'], db=db)]
            else:
                branches = [branch for branch in model.Branch.select(self.env, db=db)]
            branches.sort(key=lambda x: x.id)
            data['branches'] = branches
            data['branches_nb'] = len(branches)

        return 'branches.html', data

    def _baseline_items(self, tag_name, baselined_tags, nonbaselined_tags, db):
        """
        Explore given tag recursively and list back baselined and non-baselined tags
        :param tag_name: tag to analyze
        :type tag_name: string
        :param baselined_tags: baselined tags
        :type baselined_tags: list
        :param nonbaselined_tags: non baselined tags
        :type nonbaselined_tags: list
        """
        t = model.Tag(self.env, name=tag_name, db=db)
        if t.review or t.baselined:
            if tag_name not in baselined_tags:
                baselined_tags.append(tag_name)
                for v in model.BaselineItem.select(self.env, ['baselined_tag="' + tag_name + '"'], db=db):
                    self._baseline_items(v.name, baselined_tags, nonbaselined_tags, db)
        else:
            nonbaselined_tags.append(t)

    def _chroot(self, pathlist, pathroot):
        newpathlist = []
        for path in pathlist:
            match = re.search(r'(/trunk|/branches/B\d+)', path)
            assert match, 'A path has not the expected format'
            newpathlist.append(path.replace(match.group(1), pathroot))
        return newpathlist

    def _splitpaths(self, pathlist):
        newpathlist = []
        for path in pathlist:
            head, tail = os.path.split(path)
            if not tail:
                head, tail = os.path.split(head)
            newpathlist.append(head)
        return newpathlist

    def _makedirs(self, pathlist, pathexists, pathexclude=[]):
        for path in pathlist:
            if path not in pathexists and path not in pathexclude:
                pathexists.append(path)
                head, tail = os.path.split(path)
                if not tail:
                    head, tail = os.path.split(head)
                if head and tail and head not in pathexists:
                    self._makedirs([head], pathexists, pathexclude)

    def _logmessage(self, comment):
        # The comment string probably comes from a PC (DOS) input
        comment = comment.replace('\r', '')
        # comment is stored in a temporary file because Popen want parameters (commit message) to be coded in ASCII !
        filename = '/tmp/%s.msg' % os.getpid()
        f = codecs.open(filename, 'w', 'utf-8')
        f.write(comment)
        f.close()
        return filename

    def _rooturl(self, url):
        base_url = self.env.base_url
        base_url = base_url.replace('/tracs', '', 1)
        if util.get_repository(self.env, url).reponame:
            base_url = base_url[:base_url.rfind('/')]
        return base_url


class ComponentsAdminPanel(TagsMgmt, VersionAdminPanel):
    """ Admin panels for Components Management. """

    page_type = 'components'
    page_label = ('Component', 'Components')

    # IPermissionRequestor method

    def get_permission_actions(self):
        """ Same permissions as VersionTagsAdminPanel - Not redefined """
        return []

    # ComponentsAdminPanel methods

    def _render_admin_panel(self, req, cat, page, component):
        db = self.env.get_db_cnx()
        data = {}

        program_data = util.get_program_data(self.env)
        data['base_path'] = program_data['base_path']
        data['program_name'] = program_data['program_name']

        # Detail view?
        if component:
            comp = Component(self.env, component, db=db)

            if req.method == 'POST':
                if req.args.get('save'):
                    comp.description = req.args.get('description')
                    comp.update()
                    add_notice(req, _('Your changes have been saved.'))
                    req.redirect(req.href.admin(cat, page))
                elif req.args.get('cancel'):
                    req.redirect(req.href.admin(cat, page))

            data['component'] = comp
            data['view'] = 'detail'

        else:
            data['components'] = [comp for comp in Component.select(self.env, db=db)]
            data['view'] = 'list'

        return 'admin_components.html', data


class DocumentsAdminPanel(TagsMgmt, VersionAdminPanel):
    """ Admin panels for Documents Management. """

    page_type = 'documents'
    page_label = ('Document', 'Documents')

    # IPermissionRequestor method

    def get_permission_actions(self):
        """ Same permissions as VersionTagsAdminPanel - Not redefined """
        return []

    # DocumentsAdminPanel methods

    def _render_admin_panel(self, req, cat, page, document):
        db = self.env.get_db_cnx()
        data = {}

        program_data = util.get_program_data(self.env)
        data['base_path'] = program_data['base_path']
        data['program_name'] = program_data['program_name']
        data['trac_env_name'] = program_data['trac_env_name']

        def get_tag(tag_name):
            if tag_name:
                v = model.Tag(self.env, name=tag_name)
            else:
                v = None
            return v

        data['get_tag'] = get_tag

        # Detail view?
        if document:
            doc = model.Document(self.env, document, db=db)

            if req.method == 'POST':
                if req.args.get('save'):
                    # Class properties
                    doc['shortname'] = req.args.get('shortname')
                    doc['description'] = req.args.get('description')
                    # Instance properties
                    st = req.args.get("sourcetype", "Word:Generic Technical Document")
                    doc['sourcetype'] = st.strip()
                    doc['pdfsigned'] = 0 if "pdfsigned" not in req.args else 1
                    cc = req.args.get('controlcategory', 'N/A')
                    cc_options = self.env.config.get('ticket-custom',
                                                     'controlcategory.options')
                    cc_values = [option.strip() for option in cc_options.split('|')]
                    doc['controlcategory'] = cc_values.index(cc)
                    doc['independence'] = (
                        0 if 'independence' not in req.args else
                        1)
                    sf = req.args.get('submittedfor', 'Approval')
                    sf_options = self.env.config.get('ticket-custom',
                                                     'submittedfor.options')
                    sf_values = [option.strip() for option in sf_options.split('|')]
                    doc['submittedfor'] = sf_values.index(sf)
                    doc.save_changes(req.authname)
                    add_notice(req, _('Your changes have been saved.'))
                    req.redirect(req.href.admin(cat, page, document))
                elif req.args.get('cancel'):
                    req.redirect(req.href.admin(cat, page, filter_value=document))

            data['document'] = doc
            data['OrderedSet'] = OrderedSet
            cc_options = self.env.config.get('ticket-custom',
                                                     'controlcategory.options')
            data['controlcategory_options'] = [option.strip() for option in cc_options.split('|')]
            data["controlcategory_tip"] = web_ui.DOC_UI.controlcategory_tip
            data["doc_skill"] = util.get_doc_skill(
                self.env, document, data["program_name"]
            )
            if data["doc_skill"]:
                data['source_types'] = util.get_prop_values(self.env, "source_types")
                data['sourcetype_tip'] = web_ui.DOC_UI.sourcetype_tip(self.env)
            data['view'] = 'detail'
            sf_options = self.env.config.get('ticket-custom',
                                                     'submittedfor.options')
            data['submittedfor_options'] = [option.strip() for option in sf_options.split('|')]
            data['submittedfor_tip'] = web_ui.DOC_UI.submittedfor_tip

        else:
            # Values / parameters exchanged between the browser (HTML/JS) and the server (python)
            args = {}
            # A default value is set if not POSTING or unchecked or disabled
            args['pdf_packaging'] = req.args.get('pdf_packaging', 'true')
            args['external_refs'] = req.args.get('external_refs', 'true')
            args['pdf_renaming'] = req.args.get('pdf_renaming', 'true')
            args['prf_chklst'] = req.args.get('prf_chklst', 'false')
            args['source_files'] = req.args.get('source_files', 'false')
            # If no value is set via query parameters then a default value is set
            args['filter_value'] = req.args.get('filter_value', _('Set the appropriate filter'))
            args['selected_branch'] = req.args.get('selected_branch', 'trunk')
            args['selected_drl'] = req.args.get('selected_drl', 'Default DRL')
            args['selected_set'] = req.args.get('selected_set', 'Last version tags')
            args['max_size'] = req.args.get('max_size', '20M')
            args['ScrollX'] = req.args.get('ScrollX', 0)
            args['ScrollY'] = req.args.get('ScrollY', 0)
            args['caller'] = req.args.get('caller', None) if isinstance(req.args.get('caller'), str) else None

            # Values / parameters used internally (python code / Genshi templating)
            data['pdf_packaging'] = args['pdf_packaging'] == 'true'
            data['external_refs'] = args['external_refs'] == 'true'
            data['pdf_renaming'] = args['pdf_renaming'] == 'true'
            data['prf_chklst'] = args['prf_chklst'] == 'true'
            data['source_files'] = args['source_files'] == 'true'
            data['filter_value'] = args['filter_value']
            data['selected_branch'] = args['selected_branch']
            data['selected_drl'] = args['selected_drl']
            data['selected_set'] = args['selected_set']
            data['max_size'] = args['max_size']
            data['ScrollX'] = args['ScrollX']
            data['ScrollY'] = args['ScrollY']
            data['caller'] = args['caller']

            if req.method == "POST":
                # Update DRL
                if req.args.get('change_drl') == 'Save DRL' or req.args.get('change_drl') == 'Save DRL as':
                    # Save drl
                    drl_item_names = req.args.get('drl_item_name')  # All the checkboxes have the same name/id
                    if drl_item_names:
                        if not util.my_type(drl_item_names) == list:
                            drl_item_names = [drl_item_names]
                        drl_items = req.args.get('drl_item')
                        if drl_items:
                            if not util.my_type(drl_items) == list:
                                drl_items = [drl_items]
                        else:
                            drl_items = []
                        if req.args.get('change_drl') == 'Save DRL':
                            drl_name = data['selected_drl']
                        elif req.args.get('change_drl') == 'Save DRL as':
                            drl_name = req.args.get('drl_as')
                            if drl_name:
                                try:
                                    v = model.Drl(self.env, drl_name, db=db)
                                    add_warning(req, _("Sorry, can not create the DRL. It already exists."))
                                    req.redirect(req.href.admin(cat, page, **args))
                                except ResourceNotFound:
                                    v = model.Drl(self.env, db=db)
                                    v.name = drl_name
                                    # v.description will be input by hand
                                    v.insert(db=db)
                                    data['selected_drl'] = drl_name
                            else:
                                add_warning(req, _("Sorry, can not create the DRL. No name was specified."))
                                req.redirect(req.href.admin(cat, page, **args))
                        for drl_item_name in drl_item_names:
                            if drl_item_name in drl_items:  # Means the document is checked => add if necessary into the DRL
                                try:
                                    v = model.DrlItem(self.env, (drl_item_name, drl_name), db=db)
                                except ResourceNotFound:
                                    v = model.DrlItem(self.env, db=db)
                                    v.name = drl_item_name
                                    v.drl = drl_name
                                    v.insert(db=db)
                            else:  # Means the document is not checked => remove if necessary from the DRL
                                try:
                                    v = model.DrlItem(self.env, (drl_item_name, drl_name), db=db)
                                    v.delete(db=db)
                                except ResourceNotFound:
                                    pass
                        db.commit()
                        # Is there any document left in the DRL ?
                        if [rec.name for rec in model.DrlItem.select(self.env, ['drl = "%s"' % drl_name], db=db)]:
                            add_notice(req, _('The DRL %s has been saved.' % drl_name))
                        else:
                            v = model.Drl(self.env, drl_name, db=db)
                            v.delete(db=db)
                            db.commit()
                            add_notice(req, _('The DRL %s has been removed.' % drl_name))
                            data['selected_drl'] = 'Default DRL'

                elif req.args.get('change_drl') == 'Delete DRL':
                    # Delete drl
                    drl_name = data['selected_drl']
                    v = model.Drl(self.env, drl_name, db=db)
                    v.delete(db=db)
                    db.commit()
                    add_notice(req, _('The DRL %s has been removed.' % drl_name))
                    data['selected_drl'] = 'Default DRL'

                # Get selected documents list
                elif req.args.get('list_get') == 'Get Documents list':
                    if 'pdf_checkbox' in req.args:
                        # Creates PACKAGE_LIST_DIRECTORY if it does not exist
                        try:
                            pid = os.getpid()
                            tmp_dir = '%s/%s' % (PACKAGE_LIST_DIRECTORY, pid)
                            os.makedirs(tmp_dir)
                        except Exception:
                            pass

                        filepath = '%s/%s_%s.xlsx' % (tmp_dir, data['trac_env_name'], data['selected_set'])
                        with warnings.catch_warnings():
                            warnings.simplefilter("ignore")
                            from openpyxl import Workbook
                            wb = Workbook()
                            ws = wb.active
                            pdf_package = cache.PDFPackage(
                                    self.env,
                                    req.authname,
                                    req.args.get('pdf_checkbox'),
                                    data['pdf_renaming'],
                                    data['max_size'],
                                    data['prf_chklst'],
                                    data['source_files'])
                            for pdf_tag_file in pdf_package.pdf_tag_files:
                                ws.append([pdf_tag_file[0]])
                            wb.save(filepath)
                        req.redirect(filepath.replace('/tmp/.', '%s://%s/' % (req.scheme, util.get_hostname(self.env))))
                    else:
                        # Case where no element is selected
                        add_warning(req, _("Sorry, can not generate the documents list. No PDF was selected."))
                        req.redirect(req.href.admin(cat, page, **args))

                # Get PDF package
                elif req.args.get('pdf_get') == 'Get Documents package':
                    if 'pdf_checkbox' in req.args:
                        # Handle PDF packaging asynchronously
                        pid = os.fork()
                        if pid == 0:  # Child process
                            # The following waiting time is for allowing the parent process
                            # to end because it seems concurrency is poorly supported for
                            # access to the Trac database and generates the following error:
                            # "sqlite backend database disk image is malformed"
                            sleep(10)
                            pdf_package = cache.PDFPackage(
                                self.env,
                                req.authname,
                                req.args.get('pdf_checkbox'),
                                data['pdf_renaming'],
                                data['max_size'],
                                data['prf_chklst'],
                                data['source_files'])
                            pdf_package.build()
                            pdf_package.notify(self.compmgr)
                            os._exit(0)
                        else:  # Parent process
                            base_url = '/PDF-packaging/%s' % data['trac_env_name']
                            add_notice(req, _('You will receive an email '
                                              'when your packaging job is complete. '))
                            add_notice(req, tag(_('You may also click on '),
                                                tag.a(_('this link'), href="%s" % base_url),
                                                _(' to access the package(s) directly. '),
                                                tag.b(_('Please allow some time')),
                                                _(' for zip generation.')))
                    else:
                        # Case where no element is selected
                        add_warning(req, _("Sorry, can not generate a PDF package. No PDF was selected."))
                        req.redirect(req.href.admin(cat, page, **args))

                # Back to ECM
                elif req.args.get('pdf_get') == 'Confirm Documents selection':
                    if 'pdf_checkbox' in req.args:
                        ticket_id = data['caller'][1:]
                        ticket = Ticket(self.env, ticket_id)
                        package_name = ticket['summary']
                        pdf_package = cache.PDFPackage(
                            self.env,
                            req.authname,
                            req.args.get('pdf_checkbox'),
                            data['pdf_renaming'],
                            data['max_size'],
                            data['prf_chklst'],
                            data['source_files'],
                            package_name)
                        pdf_package.build()
                        if pdf_package.build_result == 'success':
                            ticket['documenturl'] = req.href.admin(cat, page, **args)

                            # Ticket description gives the archives content
                            # BUT without the renaming as given by selected documents
                            selected_documents = {}
                            for selected_document in [pdf.split('/') for pdf in pdf_package.pdf_list]:
                                selected_documents.setdefault(selected_document[0], []).append(selected_document[1])
                            archives_content = cache.PDFPackage.get_archives_content(ticket)
                            archives_documents = cache.PDFPackage.get_archives_documents(archives_content)
                            archives_paths = cache.PDFPackage.get_archives_documents_paths(archives_documents, selected_documents)

                            # Setup ticket description
                            ticket['description'] = ''
                            for no in archives_content.keys():
                                if ticket['description']:
                                    ticket['description'] += '\n'
                                ticket['description'] += '%s/%s.%s.zip' % (os.path.basename(pdf_package.base_dir), ticket['summary'], no)
                                for tgname in archives_content[no].keys():
                                    ticket['description'] += '\n'
                                    d_pths = []
                                    for x in range(0, len(archives_content[no][tgname])):
                                        d_pths.append(archives_paths[tgname].pop(0))
                                    ticket['description'] += '\n'.join(['%s/%s' % (tgname, d_pth) for d_pth in d_pths])
                            if len(archives_content.keys()) == 1:
                                d_zip, d_list = ticket['description'].split('\n', 1)
                                d_zip = '%s/%s.zip\n' % (os.path.basename(pdf_package.base_dir), ticket['summary'])
                                ticket['description'] = d_zip + d_list
                            now = datetime.now(utc)
                            ticket.save_changes('trac', 'Ticket changed', now)
                            template_cls = Ticket_Cache.get_subclass(ticket['type'])
                            with template_cls(self.env, data['trac_env_name'], req.authname, ticket) as doc:
                                # Force Lock mode
                                doc.create_flag(ticket['sourcefile'], 'go')
                            req.redirect(req.href.ticket(ticket_id))
                        else:
                            raise TracError(tag.p(pdf_package.build_message, class_="message"))
                    else:
                        # Case where no element is selected
                        add_warning(req, _("Sorry, no PDF has been selected. Previous selection has been restored."))
                        req.redirect(req.href.admin(cat, page, **args))

                # Apply filter
                elif req.args.get('update'):
                    pass  # only redirection with filter_value as a parameter

                req.redirect(req.href.admin(cat, page, **args))

            # All known branches
            data['branch_segregation_activated'] = True if self.env.config.get('artusplugin', 'branch_segregation_activated') == 'True' else False
            if data['branch_segregation_activated']:
                data['branches'] = ['trunk'] + ['B%s' % branch.id for branch in model.Branch.select(self.env, db=db)]

            # All known drls
            data['drls'] = ['Default DRL'] + [drl.name for drl in model.Drl.select(self.env, None, db=db)]

            # Filter
            where_expr_list = ['name LIKE "%' + data['filter_value'] + '%"']

            # documents listed (filter applied)
            data['documents'] = OrderedDict()
            for doc in model.Document.select(self.env, where_expr_list, db=db):
                if data['selected_drl'] == 'Default DRL':
                    inDrl = True
                else:
                    try:
                        model.DrlItem(self.env, (doc['name'],
                                                 data['selected_drl']),
                                      db=db)
                        inDrl = True
                    except ResourceNotFound:
                        inDrl = False
                data['documents'][doc['name']] = {'description': doc['description'],
                                                  'inDrl': inDrl,
                                                  'sourcetype': doc['sourcetype'],
                                                  'pdfsigned': doc['pdfsigned'],
                                                  'independence': doc['independence'],
                                                  'controlcategory': doc['controlcategory'],
                                                  'inVtg': False}

            # Baselines and milestones with applied document(s) are listed
            # If branches are segregated, only baselines and milestones
            # with document(s) on the selected branch will be listed
            version_tags = [tg for tg in model.Tag.select(
                self.env,
                ['baselined = 1', 'tag_url IS NOT NULL'],
                db=db, tag_type=VersionTagsAdminPanel.page_type)]
            version_tags += [tg for tg in model.Tag.select(
                self.env,
                ['review IS NOT NULL', 'tag_url IS NOT NULL'],
                db=db, tag_type=MilestoneTagsAdminPanel.page_type)]
            if data['branch_segregation_activated']:
                select_tags = []
                for vtg in version_tags:
                    seen_doc_versions = get_docs_from_including_tag(
                        self.env, vtg.name, set(), set(), external_refs=data['external_refs'])[0]
                    if seen_doc_versions:
                        for doc_version_tag in zip(*seen_doc_versions)[0]:
                            tg = model.Tag(self.env, doc_version_tag)
                            if NamingRule.get_branch_from_tag(self.env, tg.name) == data['selected_branch']:
                                select_tags.append(vtg)
                                break
            else:
                select_tags = version_tags

            if select_tags:
                # Tags are grouped by 'baselines/milestones'
                # then 'tagged_item' and finally by 'status'
                data['select_tags'] = util.group_by(select_tags, ('baselined', False), ('tagged_item', False), ('status', True))
            else:
                # No baseline or milestone
                data['select_tags'] = None

            # Years with applied document(s) are listed
            # If branches are segregated, only years
            # with document(s) on the selected branch will be listed
            doc_tags = [tg for tg in model.Tag.select(
                self.env,
                ['component = 0', 'tag_url IS NOT NULL'],
                db=db, tag_type=VersionTagsAdminPanel.page_type)]
            if data['branch_segregation_activated']:
                doc_tags = [tg for tg in doc_tags if NamingRule.get_branch_from_tag(self.env, tg.name) == data['selected_branch']]
            if doc_tags:
                data['select_years'] = sorted(list({util.get_date(self.env, tg.tag_url).year for tg in doc_tags}), reverse=True)
            else:
                # No years
                data['select_years'] = None

            # Last APPLIED version for all known documents
            last_version_tags = {}
            for docname in data['documents'].keys():
                applied_tags = get_applied_tags(self.env, docname, 'document')
                if data['branch_segregation_activated']:
                    applied_tags = [tg for tg in applied_tags if NamingRule.get_branch_from_tag(self.env, tg.name) == data['selected_branch']]
                if applied_tags:
                    last_version_tags[docname] = applied_tags[-1].name
                else:
                    last_version_tags[docname] = None
            data['last_version_tags'] = last_version_tags

            def get_pdf_version_tag(doc_version_tag, select):
                pdf_version_tag = {'doc_version_tag': doc_version_tag}
                if doc_version_tag:
                    doc_tag = model.Tag(self.env, doc_version_tag)
                    pdf_version_tag['pdf_data'] = cache.PDFPackage.get_pdf_files(
                        self.env,
                        doc_tag.tag_url,
                        select)
                    if pdf_version_tag['pdf_data']:
                        if select:
                            tkid = util.get_doc_tktid(self.env, doc_tag.tagged_item)
                            if tkid:
                                ticket = Ticket(self.env, tkid)
                                pdffile = ticket['pdffile']
                                if pdffile and pdffile != 'N/A':
                                    for counter, item in enumerate(pdf_version_tag['pdf_data']):
                                        if item[0] == pdffile:
                                            pdf_version_tag['pdf_data'][counter] = (item[0], True)
                                        else:
                                            pdf_version_tag['pdf_data'][counter] = (item[0], False)
                    else:
                        pdf_version_tag['pdf_data'] = [('No pdf', False)]
                else:
                    pdf_version_tag['pdf_data'] = [('N/A', False)]

                return pdf_version_tag

            pdf_version_tags = {}
            set_selection = data['selected_set'] and data['selected_set'] != 'Last version tags'

            # A default list of PDF files is established.
            # If no baseline or milestone is selected,
            # the selected ones are determined
            # else none are selected, they will be selected below
            for tracked_item in data['last_version_tags']:
                doc_version_tag = data['last_version_tags'][tracked_item]
                pdf_version_tags[tracked_item] = get_pdf_version_tag(doc_version_tag,
                                                                     not set_selection)

            if set_selection:
                if util.is_int(data['selected_set']):
                    # A year has been selected,
                    # another list of PDF files is established
                    # from the selected year and
                    # the selected ones are determined
                    year = int(data['selected_set'])
                    for docname in data['documents'].keys():
                        applied_tags = [tg for tg in get_applied_tags(self.env, docname, 'document') if util.get_date(self.env, tg.tag_url).year == year]
                        if data['branch_segregation_activated']:
                            applied_tags = [tg for tg in applied_tags if NamingRule.get_branch_from_tag(self.env, tg.name) == data['selected_branch']]
                        if applied_tags:
                            pdf_version_tags[docname] = get_pdf_version_tag(applied_tags[-1].name, True)
                            data['documents'][docname]['inVtg'] = True
                else:
                    # A baseline or milestone has been selected,
                    # another list of PDF files is established
                    # from the baseline or milestone and
                    # the selected ones are determined
                    seen_doc_versions = get_docs_from_including_tag(
                        self.env, data['selected_set'], set(), set(), external_refs=data['external_refs'])[0]
                    if seen_doc_versions:
                        # list is sorted automatically by each tuple's first element
                        ordered_doc_versions = sorted(seen_doc_versions)
                        # Group document versions by documents (tracked_item)
                        # As document versions are sorted,
                        # only the most recent one from the baseline or milestone
                        # will be kept in the end
                        for doc_version_tag in zip(*ordered_doc_versions)[0]:
                            tg = model.Tag(self.env, doc_version_tag)
                            if data['branch_segregation_activated'] and NamingRule.get_branch_from_tag(self.env, tg.name) == data['selected_branch']:
                                tracked_item = tg.tracked_item
                                if tracked_item in data['last_version_tags']:
                                    pdf_version_tags[tracked_item] = get_pdf_version_tag(doc_version_tag, True)
                                    data['documents'][tracked_item]['inVtg'] = True

            data['pdf_version_tags'] = pdf_version_tags

            in_drl = 0
            for docname in data['documents'].keys():
                if data['documents'][docname]['inDrl']:
                    in_drl += 1

            data['in_drl'] = in_drl

            selected_pdf = 0
            for docname in data['documents'].keys():
                for pdf in data['pdf_version_tags'][docname]['pdf_data']:
                    if (data['pdf_version_tags'][docname]['doc_version_tag'] and
                        data['documents'][docname]['inDrl'] and pdf[1]):
                        selected_pdf += 1

            data['ticket_types'] = [tkt_type.name for tkt_type in Type.select(self.env)]
            data['selected_pdf'] = selected_pdf
            data['get_ecm_tktid'] = util.get_ecm_tktid
            data['get_ecm_tktstatus'] = util.get_ecm_tktstatus
            data['get_doc_tktid'] = util.get_doc_tktid
            data['get_doc_tktstatus'] = util.get_doc_tktstatus
            data['get_doc_query_string'] = util.get_doc_query_string
            data['get_doc_skill'] = util.get_doc_skill
            data['get_version_from_tag'] = NamingRule.get_version_from_tag
            data['is_int'] = util.is_int
            data['Tag'] = model.Tag

            data['pdf_checkbox'] = {}
            for docname in data['documents']:
                data['pdf_checkbox'][docname] = {}
                for pdf in data['pdf_version_tags'][docname]['pdf_data']:
                    data['pdf_checkbox'][docname][pdf[0]] = (data['documents'][docname]['inDrl'] and pdf[1]) or None

            # Selected PDFs may be overwritten by ECM description if coming from ECM
            if req.get_header('Referer') and data["caller"] and re.match(r"t\d+", data["caller"]):
                try:
                    ECM_tkt = Ticket(self.env, data['caller'][1:])
                    if ECM_tkt['description']:
                        selected_documents = {}
                        # Get PDFs from ticket description
                        for elt in [path.split('/') for path in ECM_tkt['description'].split('\n')]:
                            # Only PDF under root folder are considered
                            if len(elt) == 2 and elt[1].lower().endswith('.pdf'):
                                selected_documents.setdefault(elt[0], []).append(elt[1])
                        # Use tracked_item as key
                        for vtag_name in selected_documents.keys():
                            try:
                                tracked_item = get_tag(vtag_name).tracked_item
                                selected_documents[tracked_item] = selected_documents.pop(vtag_name)
                            except ResourceNotFound:
                                selected_documents.pop(vtag_name)
                        # Setup selected PDFs
                        for docname in data['documents'].keys():
                            data['pdf_checkbox'][docname] = {}
                            for pdf in data['pdf_version_tags'][docname]['pdf_data']:
                                data['pdf_checkbox'][docname][pdf[0]] = True if (docname in selected_documents and pdf[0] in selected_documents[docname]) else None
                except ResourceNotFound:
                    pass

        return 'documents.html', data


class MilestoneTagsAdminPanel(TagsMgmt):
    """ Admin panels for Milestone Tags Management. """

    page_type = 'milestone_tags'
    page_label = ('Milestone Tag', 'Milestone Tags')
    _actions = ['MILESTONE_TAG_VIEW', 'MILESTONE_TAG_CREATE', 'MILESTONE_TAG_MODIFY', 'MILESTONE_TAG_APPLY', 'MILESTONE_TAG_DELETE']
    _admin_privilege = 'MILESTONE_TAG_ADMIN'

    @staticmethod
    def get_mom_tktstatus(env, milestonetag):
        db = env.get_db_cnx()
        tg = model.Tag(env, name=milestonetag, db=db)
        tktid = util.get_mom_tktid(env, tg)
        if tktid:
            tkt = Ticket(env, tktid)
            return tkt['status']
        else:
            return None

    @staticmethod
    def get_mom_skill(env, milestonetag, prefix):
        skill = util.get_skill(env, milestonetag, prefix)
        return skill

    @staticmethod
    def is_milestone_accepted(env, milestonetag):
        db = env.get_db_cnx()
        accepted_milestones = [v.tagged_item
                               for v in model.Tag.select(
                                   env,
                                   ['status="Accepted"',
                                    'tag_url IS NOT NULL'],
                                   db=db,
                                   tag_type='milestone_tags')]
        if model.Tag(env, milestonetag).tagged_item in accepted_milestones:
            return True
        else:
            return False

    # IPermissionRequestor method

    def get_permission_actions(self):
        """ Definition of rights regarding milestone tags management
        MILESTONE_TAG_xxx : rights to view, create, modify, apply or delete a milestone tag

        E.g.:
                                authenticated      developer    authorized      admin
        MILESTONE_TAG_VIEW             X               X             X            X
        MILESTONE_TAG_CREATE                                         X            X
        MILESTONE_TAG_MODIFY                                         X            X
        MILESTONE_TAG_APPLY                                          X            X
        MILESTONE_TAG_DELETE                                         X(*)         X
        MILESTONE_TAG_ADMIN                                                       X

        (*) A milestone tag can be deleted by authorized user only if not yet applied

        'applied' means 'tag created in the repository'

        """
        return self._actions + [(self._admin_privilege, self._actions)]

    # MilestoneTagsAdminPanel methods

    def _render_admin_panel(self, req, cat, page, milestone_tag):

        db = self.env.get_db_cnx()
        data = init_data(self, req)
        data['ticket_types'] = [ttype.name for ttype in Type.select(self.env)]
        data['milestone_tag'] = model.Tag(self.env, milestone_tag, db=db)

        # Detail view?
        if milestone_tag:
            self._render_detail_view(req, cat, page, milestone_tag, data, db)
        else:
            self._render_main_view(req, cat, page, milestone_tag, data, db)

        return 'milestone_tags.html', data

    def _render_detail_view(self, req, cat, page, milestone_tag, data, db):
        data['tg'] = data['milestone_tag']
        tag_url = (data['tg'].source_url or '') + '/tags/milestones/' + util.get_prop_values(self.env, 'skill_dirs')[data['tg'].tracked_item] + '/' + data['tg'].status + '/' + data['tg'].name
        data['tag_url'] = tag_url

        if req.method == 'POST':
            self._render_detail_POST(req, cat, page, milestone_tag, data, db)

        self._render_detail_GET(req, cat, page, milestone_tag, data, db)

    def _render_detail_POST(self, req, cat, page, milestone_tag, data, db):
        # Milestone Tag Reqtify project and export script
        if req.args.get('reqtify_project'):

            upload = req.args['reqtify_project_file']
            filename = util.upload_filename(upload, 'rqtf')
            if not filename:
                raise TracError(_('No file uploaded'))
            filepath = '%s/%s' % (REQTIFY_PROJECT_DIRECTORY, filename)
            rqtf = ReqtifyProject(self.env, upload.file, filepath)
            # Get documents described in the Reqtify project
            document_pattern = r"\A(" + data['program_name'] + \
                r"_(?:%s)(?:_(?:[^\W_]|-)+)?_(?:(?:[^\W_]|-)+))" \
                % self.env.config.get('ticket-custom', 'skill.options')
            reqtify_docs = rqtf.read(document_pattern)
            # Get baseline docs
            baseline_docs = {}
            seen_doc_versions = get_docs_from_including_tag(self.env, milestone_tag, set(), set())[0]
            # list is sorted automatically by each tuple's first element
            ordered_doc_versions = sorted(seen_doc_versions)
            # Group document versions by documents (tracked_item)
            # As document versions are sorted,
            # only the most recent one will be kept en the end
            for doc_version, subpath in ordered_doc_versions:
                doc_tag = data['get_tag'](doc_version)
                tracked_item = doc_tag.tracked_item
                tag_url = data['get_url'](doc_tag.tag_url)
                baseline_docs[tracked_item] = (doc_version, subpath, tag_url)
            # Update Reqtify doc paths
            rexp = '.*?%s(?:/(?:Draft|Proposed|Released)/%s)?(?:_\d+\.\d+\.(?:Draft|Proposed|Released)\d*)?(.*)'
            rqtf.update(reqtify_docs, baseline_docs, rexp, milestone_tag)
            # Writes back Reqtify project
            rqtf.write(reqtify_docs)
            # Generates export script
            batchpath = '%s/%s.bat' % (REQTIFY_PROJECT_DIRECTORY, filename.split('.')[0])
            tag_url = data['get_tag'](milestone_tag).tag_url
            rqtf.export(milestone_tag, tag_url, baseline_docs, reqtify_docs, batchpath)
            # Packages the Reqtify project file
            zippath = '%s/%s.zip' % (REQTIFY_PROJECT_DIRECTORY, filename.split('.')[0])
            util.create_archive_file(self.env, [rqtf.filepath, batchpath], zippath)
            req.redirect(zippath.replace('/tmp/.', '%s://%s/' % (req.scheme, util.get_hostname(self.env))))

        # Milestone Tag Index
        elif req.args.get('listing'):
            if data['tg'].tag_url:
                target_url = util.get_url(util.get_repo_url(self.env, data['tg'].tag_url))
                filepath = generate_tag_index(self.env, milestone_tag, target_url, data['program_name'], req.authname)
                req.redirect(filepath.replace('/tmp/.', '%s://%s/' % (req.scheme, util.get_hostname(self.env))))
            else:
                raise TracError(tag.p("Only applied tags can be listed", class_="message"))

        # External references
        elif req.args.get('save_refs'):
            data['tg'].tag_refs = req.args.get('refs')
            data['tg'].update(db=db)
            db.commit()

        # Milestone Tag Export Script
        elif req.args.get('export_script'):
            if data['tg'].tag_url:
                target_url = util.get_url(util.get_repo_url(self.env, data['tg'].tag_url))
                filepath = generate_tag_index(self.env, milestone_tag, target_url, data['program_name'], req.authname)
                req.redirect(filepath.replace('/tmp/.', '%s://%s/' % (req.scheme, util.get_hostname(self.env))))
            else:
                raise TracError(tag.p("Only applied tags can be exported", class_="message"))

        # Apply filter:
        elif req.args.get('update'):
            req.redirect(req.href.admin(cat, page, milestone_tag, filter_value=data['filter_value'], sort_including=req.args.get('sort_including'), asc_including=req.args.get('asc_including'), sort_included=req.args.get('sort_included'), asc_included=req.args.get('asc_included'), ScrollX=req.args.get('ScrollX'), ScrollY=req.args.get('ScrollY'), selected_item=req.args.get('selected_item'), included_tag=model.simplify_whitespace(req.args.get('included_tag'))))

        # Include Tag into Baseline
        elif req.args.get('add') and req.args.get('included_tag'):
            included_tag = model.simplify_whitespace(req.args.get('included_tag'))

            try:
                model.BaselineItem(self.env, (included_tag, milestone_tag), db=db)
            except ResourceNotFound:
                if req.args.get('replaced_tag'):
                    # 'replaced_tag' is removed from Baseline
                    v = model.BaselineItem(self.env, (req.args.get('replaced_tag'), milestone_tag), db=db)
                    v.delete(db=db)
                    db.commit()

                # 'included_tag' is included into Baseline
                v = model.BaselineItem(self.env, db=db)
                v.name = included_tag
                v.baselined_tag = milestone_tag
                v.author = req.authname
                # v.subpath not used in this context
                v.insert(db=db)
                db.commit()
                req.redirect(req.href.admin(cat, page, milestone_tag, filter_value=data['filter_value'], sort_including=req.args.get('sort_including'), asc_including=req.args.get('asc_including'), sort_included=req.args.get('sort_included'), ScrollX=req.args.get('ScrollX'), ScrollY=req.args.get('ScrollY'), selected_item=included_tag, included_tag=included_tag))
            else:
                w = model.Tag(self.env, included_tag, db=db)
                if w.review is None:
                    included_page = VersionTagsAdminPanel.page_type
                else:
                    included_page = MilestoneTagsAdminPanel.page_type
                raise TracError(tag.p(tag.em("Tag "), tag.a("%s" % included_tag, href=req.href.admin(cat, included_page, included_tag)), " already included in ", tag.a("%s" % milestone_tag, href=req.href.admin(cat, page, milestone_tag)), class_="message"))

        # Remove Tag from Baseline
        elif req.args.get('remove'):
            sel = req.args.get('sel')
            if not sel:
                raise TracError(_('No Included Tag selected'))
            if not isinstance(sel, list):
                sel = [sel]
            for name in sel:
                v = model.BaselineItem(self.env, (name, milestone_tag), db=db)
                v.delete(db=db)
            db.commit()
            req.redirect(req.href.admin(cat, page, milestone_tag, filter_value=data['filter_value'], sort_including=req.args.get('sort_including'), asc_including=req.args.get('asc_including'), sort_included=req.args.get('sort_included'), ScrollX=req.args.get('ScrollX'), ScrollY=req.args.get('ScrollY'), selected_item=req.args.get('selected_item'), included_tag=req.args.get('included_tag')))

        # Apply Tag into the repository
        elif req.args.get('apply'):
            paths, dirs = util.analyse_url(self.env, data['tag_url'])
            # comment string
            if req.args.get('comment'):
                comment = req.args.get('comment')
            else:
                comment = "tag %s" % data['tg'].name
            # The comment string probably comes from a PC (DOS) input
            comment = comment.replace('\r', '')
            comment += _(' (on behalf of %(user)s)', user=req.authname)
            # comment is stored in a temporary file because Popen want parameters (commit message) to be coded in ASCII !
            msg_filename = '/tmp/%s.msg' % os.getpid()
            f = codecs.open(msg_filename, 'w', 'utf-8')
            f.write(comment)
            f.close()
            svnmucc_cmd = util.SVNMUCC_TEMPLATE_CMD + '-F "%s" ' % msg_filename
            for d in dirs:
                svnmucc_cmd += 'mkdir "%s" ' % d
            svnmucc_cmd += 'mkdir "%s" ' % (paths[-2] + '/' + milestone_tag)
            for v in [v for v in model.BaselineItem.select(self.env, ['baselined_tag="' + milestone_tag + '"'], db=db)]:
                vv = model.Tag(self.env, name=v.name, db=db)
                if vv.review is None:
                    included_page = VersionTagsAdminPanel.page_type
                else:
                    included_page = MilestoneTagsAdminPanel.page_type
                if vv.tag_url is None:
                    raise TracError(tag.p(tag.em("Milestone tag "), tag.a("%s" % milestone_tag, href=req.href.admin(cat, page, milestone_tag)), " could not be applied because included ", tag.em("tag "), tag.a("%s" % vv.name, href=req.href.admin(cat, included_page, vv.name)), " has not been applied", class_="message"))
                else:
                    included_tag_url = vv.tag_url
                    included_tag_revision = util.get_revision(included_tag_url)
                    if included_tag_revision == '':
                        raise TracError(tag.p(tag.em("Milestone tag "), tag.a("%s" % milestone_tag, href=req.href.admin(cat, page, milestone_tag)), " could not be applied because ", tag.em("tag "), tag.a("%s" % vv.name, href=req.href.admin(cat, included_page, vv.name)), " has an url without revision", class_="message"))
                    included_tag_url = util.get_url(included_tag_url)
                    svnmucc_cmd += 'cp %s "%s" ' % (included_tag_revision, util.get_repo_url(self.env, included_tag_url)) + '"%s" ' % (paths[-2] + '/' + milestone_tag + '/' + vv.name)
            unix_cmd_list = [svnmucc_cmd]
            retcode, lines = util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())
            # Temporary commit message file is removed
            try:
                os.remove(msg_filename)
            except os.error:
                pass
            # Result of UNIX commands
            if retcode != 0:
                message = tag.p("Applying of ", tag.em("milestone tag "), tag.a("%s" % data['tg'].name, href=req.href.admin(cat, page, data['tg'].name)), " has failed.", class_="message")
                for line in lines:
                    message(tag.p(line))
                raise TracError(message)
            else:
                match = re.match(r'^r(\d+) committed by trac at', lines[0])
                if match:
                    tag_revision = match.group(1)
                    data['tg'].tag_url = data['tag_url'] + '?rev=' + tag_revision
                    data['tg'].author = req.authname
                    data['tg'].update(db=db)
                    # The associated milestone is timed if it's status is 'Accepted'
                    if data['tg'].status == 'Accepted':
                        w = Milestone(self.env, name=data['tg'].tagged_item, db=db)
                        w.completed = datetime.now(utc)
                        w.update(db=db)
                    db.commit()
                    add_notice(req, tag(_('The tag '),
                                        tag.a('%s' % milestone_tag,
                                              href="%s" % req.href.admin(cat, page, milestone_tag)),
                                        _(' has been applied in the repository as revision '),
                                        tag.a('%s' % util.get_revision(data['tg'].tag_url),
                                              href="%s" % util.get_tracbrowserurl(self.env, util.get_url(data['tg'].tag_url))),
                                        '.'))
                    req.redirect(req.href.admin(cat, page, selected_item=milestone_tag, filter_value=milestone_tag))
                else:
                    message = tag.p("Applying of ", tag.em("milestone tag "), tag.a("%s" % data['tg'].name, href=req.href.admin(cat, page, data['tg'].name)), " has failed.", class_="message")
                    message(tag.p("Could not find revision associated with successful commit"))
                    raise TracError(message)

        # Cancel
        elif req.args.get('cancel'):
            req.redirect(req.href.admin(cat, page, selected_item=milestone_tag, filter_value=milestone_tag))

    def _render_detail_GET(self, req, cat, page, milestone_tag, data, db):
        data['view'] = 'detail'
        data['tag_refs'] = data['tg'].tag_refs
        data['refs_syntax'] = ("**Syntax**:[[br]][[br]]"
                               "`<program id>:<milestone tag>` or `<program id>:<version tag>`[[br]][[br]]"
                               "__Note__: If the tag is in this TRAC database, the program id  - %s - can be omitted, as follows:[[br]][[br]]"
                               "`<milestone tag>` or `<version tag>`") % data['program_name']

        db = self.env.get_db_cnx()
        tg = model.Tag(self.env, name=milestone_tag, db=db)
        mom_tktid = util.get_mom_tktid(self.env, tg)

        # View associated Review MOM
        if data['tg'].status == 'Reviewed':
            if mom_tktid:
                add_ctxtnav(req, _('View Associated Review MOM'), href=req.href('ticket', mom_tktid))

        # View associated CCB MOM
        if data['tg'].status == 'Prepared':
            if mom_tktid:
                add_ctxtnav(req, _('View Associated CCB MOM'), href=req.href('ticket', mom_tktid))

        # View associated milestone tags
        if 'MILESTONE_TAG_VIEW' in data['perm']:
            add_ctxtnav(req, _('View Associated Milestone Tags'), href=req.href.admin(cat, page, filter_value=data['tg'].tagged_item))

        # View associated milestone
        if 'MILESTONE_TAG_VIEW' in data['perm']:
            add_ctxtnav(req, _('View Associated Milestone'), href='%s/admin/tags_mgmt/milestones/%s' % (data['base_path'], data['tg'].tagged_item))

        # Baseline empty ?
        if len([v.name for v in model.BaselineItem.select(self.env, ['baselined_tag="' + milestone_tag + '"'], db=db)]) == 0:
            data['empty_baseline'] = True
        else:
            data['empty_baseline'] = False

        # Filter analysis
        filter_value = data['filter_value']
        where_expr_list = []
        if filter_value.startswith('*'):
            filter_value = filter_value[1:]
            where_expr_list += ['baselined=1', 'buildbot=0']
        if filter_value.endswith(']'):
            filter_value = filter_value[:-1]
            where_expr_list += ['tracked_item LIKE "%' + filter_value + '"']
        else:
            where_expr_list += ['name LIKE "%' + filter_value + '%"']

        # Only one tag in the baseline for each tracked item
        already_included_tags = [v.name for v in model.BaselineItem.select(self.env, ['name LIKE "%' + filter_value + '%"', 'baselined_tag="' + milestone_tag + '"'], db=db)]

        # The tags to be included must have an associated tracked_item different from that of the baseline which I am currently defining
        my_tracked_item = data['tg'].tracked_item

        # List of tags susceptible to be included into the baseline
        tags = [v for v in model.Tag.select(self.env, ['name LIKE "%' + filter_value + '%"'], db=db, tag_type=self.page_type) if v.name not in already_included_tags and v.tracked_item != my_tracked_item and v.tag_url]
        tags += [v for v in model.Tag.select(self.env, where_expr_list + ['component=1'], db=db, tag_type=VersionTagsAdminPanel.page_type) if v.name not in already_included_tags and v.tracked_item != my_tracked_item and v.tag_url]
        tags += [v for v in model.Tag.select(self.env, where_expr_list + ['component=0'], db=db, tag_type=VersionTagsAdminPanel.page_type) if v.name not in already_included_tags and v.tracked_item != my_tracked_item and v.tag_url and not v.tracked_item.startswith('ECM_%s_' % data['program_name'])]
        tags = get_sorted_tags_by_rev(tags)
        data['included_tags_nb'] = len(tags)

        if tags:
            # Tags are grouped by 'tagged_item' and by 'status'
            data['included_tags'] = util.group_by(tags, ('tagged_item', False), ('status', True))
        else:
            data['included_tags'] = None

        if req.args.get('included_tag') and req.args.get('included_tag') in [v.name for v in tags]:
            data['included_tag'] = req.args.get('included_tag')
        elif data['included_tags']:
            data['included_tag'] = tags[-1].name
        else:
            data['included_tag'] = None

        if data['included_tag']:

            tracked_item = data['get_tag'](data['included_tag']).tracked_item

            already_included_tracked_items = {}
            for already_included_tag in already_included_tags:
                already_included_tracked_item = data['get_tag'](already_included_tag).tracked_item
                already_included_tracked_items[already_included_tracked_item] = already_included_tag

            if tracked_item in already_included_tracked_items:
                data['replaced_tag'] = already_included_tracked_items[tracked_item]
            else:
                data['replaced_tag'] = None

    def _render_main_view(self, req, cat, page, milestone_tag, data, db):
        if req.method == "POST":
            self._render_main_POST(req, cat, page, milestone_tag, data, db)

        self._render_main_GET(req, cat, page, milestone_tag, data, db)

    def _render_main_POST(self, req, cat, page, milestone_tag, data, db):
        # Create Tag
        if req.args.get('add') and req.args.get('tag_name'):
            tag_name = req.args.get('tag_name')
            try:
                model.Tag(self.env, name=tag_name, db=db)
            except ResourceNotFound:
                v = model.Tag(self.env, db=db)
                v.name = tag_name
                v.tagged_item = req.args.get('milestone_name')
                v.tracked_item = req.args.get('skill')
                v.author = req.authname
                v.review = req.args.get('review')
                # v.standard not used in this context
                # v.edition not used in this context
                # v.revision not used in this context
                v.status = req.args.get('status')
                v.status_index = req.args.get('status_index')
                v.source_url = req.args.get('repos') and '/' + req.args.get('repos') or None
                # v.tag_url not known at this stage
                v.component = 0  # not used in this context but may not be NULL
                v.baselined = 0  # not used in this context but may not be NULL
                v.buildbot = 0  # not used in this context but may not be NULL
                v.version_type = 0  # not used in this context but may not be NULL
                tg = model.Tag(self.env, name=req.args.get('from_tag'), db=db)
                v.tag_refs = tg.tag_refs
                v.insert(db=db)
                for from_tag in [from_tag for from_tag in model.BaselineItem.select(self.env, ['baselined_tag="' + model.simplify_whitespace(req.args.get('from_tag')) + '"'], db=db)]:
                    vv = model.BaselineItem(self.env, db=db)
                    vv.name = from_tag.name
                    vv.baselined_tag = tag_name
                    vv.author = req.authname
                    # vv.subpath not used in this context
                    vv.insert(db=db)
                # An entry is added in the milestone table if not already in it
                try:
                    Milestone(self.env, name=v.tagged_item, db=db)
                except ResourceNotFound:
                    w = Milestone(self.env, db=db)
                    w.name = v.tagged_item
                    # w.due will be input by hand
                    # w.completed will by input hand
                    # w.description will be input by hand
                    w.insert(db=db)
                    # Removes the 'Dummy' milestone if still there
                    try:
                        x = Milestone(self.env, name='Dummy', db=db)
                        x.delete(db=db)
                    except ResourceNotFound:
                        pass
                db.commit()

                # custom field milestonetag options are updated
                milestonetags = "|".join([x.name for x in
                                          model.Tag.select(self.env,
                                                           db=db,
                                                           tag_type=self.page_type)])
                # field may be empty
                if milestonetags:
                    milestonetags = "|" + milestonetags

                self.env.config.set('ticket-custom', 'milestonetag.options', milestonetags)

                try:
                    self.env.config.save()
                except Exception:
                    e = sys.exc_info()[1]
                    try:
                        self.log.error('Error writing to trac.ini: %s',
                                    exception_to_unicode(e))
                        add_warning(req, _('Error writing to trac.ini, '
                                        'make sure it is writable by '
                                        'the web server. Your change '
                                        'has not been saved.'))
                    finally:
                        del e

                req.redirect(req.href.admin(cat, page, tag_name))
            else:
                raise TracError(tag.p(tag.em("Milestone tag "),
                                      tag.a("%s" % tag_name,
                                            href=req.href.admin(cat, page, tag_name)),
                                      " already exists.", class_="message"))

        # Remove Tag
        elif req.args.get('remove'):
            sel = req.args.get('sel')
            if not sel:
                raise TracError(_('No milestone tag selected'))
            if not isinstance(sel, list):
                sel = [sel]

            # The milestone tags can only be removed if each of them is not included in a baseline not removed itself
            blocking = []
            for name in sel:
                blocking += [(v.name, v.baselined_tag) for v in model.BaselineItem.select(self.env, ['name="' + name + '"'], db=db) if v.baselined_tag not in sel]
            if blocking:
                message = tag.p("Either remove the listed baselined tags or remove the problematic included tags from them (if the baselined tags are not yet tagged).", class_="message")
                for block in blocking:
                    w = model.Tag(self.env, block[1], db=db)
                    if w.tag_url is None:
                        tagged = 'not yet applied '
                    else:
                        tagged = 'applied '
                    message(tag.p("Can't remove ", tag.em("tag "), tag.a("%s" % block[0], href=req.href.admin(cat, page, block[0])), " because it is included in ", tag.b(tagged), tag.em("baselined tag "), tag.a("%s." % block[1], href=req.href.admin(cat, page, block[1], selected_item="%s" % block[0]))))
                raise TracError(message, "Cannot delete Tag(s) included in Baselines")

            # The milestone tags can only be removed if each of them is not used as milestone tag in one or more ticket(s)
            blocking = []
            for name in sel:
                cursor = db.cursor()
                cursor.execute("SELECT ticket FROM ticket_custom WHERE name='milestonetag' and value='%s'" % name)
                tkt_ids = [int(row[0]) for row in cursor]
                if tkt_ids:
                    blocking.append((name, len(tkt_ids)))
            if blocking:
                message = tag.p("If feasible, change the milestone tag(s) for the ticket(s) involved.", class_="message")
                for block in blocking:
                    message(tag.p("Can't remove ", tag.em("milestone tag "), tag.a("%s" % block[0], href=req.href.admin(cat, page, block[0])), " because it is used as a milestone tag in ", tag.a("%d " % block[1], href=req.href.query(group="status", milestonetag="%s" % block[0], order="priority", col=["id", "summary", "type", "milestonetag"])), "ticket(s)."))
                raise TracError(message, "Cannot delete tag(s) used as milestone tag(s) in one ore more ticket(s)")

            # The milestone tags can only be removed if the associated milestone is not used in one or more ticket(s) OR there will remain at least one milestone tag
            blocking = []
            for name in sel:
                v = model.Tag(self.env, name, db=db)
                mil = v.tagged_item
                cursor = db.cursor()
                cursor.execute("SELECT id FROM ticket WHERE milestone='%s'" % mil)
                tkt_ids = [int(row[0]) for row in cursor]
                if tkt_ids:
                    blocking.append((name, len(tkt_ids), mil))
            if blocking:
                remaining_milestone_tags = [w.name for w in model.Tag.select(self.env, ['tagged_item="' + mil + '"'], db=db, tag_type=self.page_type) if w.name not in sel]
                if len(remaining_milestone_tags) == 0:
                    message = tag.p("If feasible, do not delete all the milestone tag(s) or change the milestone(s) for the ticket(s) involved.", class_="message")
                    for block in blocking:
                        message(tag.p("Can't remove ", tag.em("milestone tag "), tag.a("%s" % block[0], href=req.href.admin(cat, page, block[0])), " along with all other milestone tags because the associated milestone is used in ", tag.a("%d " % block[1], href=req.href.query(group="status", milestone="%s" % block[2], order="priority", col=["id", "summary", "type", "milestone"])), "ticket(s)."))
                    raise TracError(message, "Cannot delete all the selected tag(s) and therefore the associated milestone because it is used in one ore more ticket(s)")

            # The milestone tags can only be removed if each of them is not used as baseline for branching
            blocking = []
            for name in sel:
                blocking += [(branch.source_tag, branch.id) for branch in model.Branch.select(self.env, ['source_tag="' + name + '"'], db=db)]
            if blocking:
                message = tag.p("If feasible, remove the listed branches or change the source tag of them (if the branches are not yet applied).", class_="message")
                for block in blocking:
                    w = model.Branch(self.env, block[1], db=db)
                    if w.branch_url is None:
                        tagged = '- not yet applied - '
                    else:
                        tagged = '- applied - '
                    message(tag.p("Can't remove ", tag.em("milestone tag "), tag.a("%s" % block[0], href=req.href.admin(cat, page, block[0])), " because it is the source tag of ", tagged, tag.em("branch "), tag.a("B%s." % block[1], href=req.href.admin(cat, 'branches', block[1]))))
                raise TracError(message, "Cannot delete tag(s) used as source tags in one or more branch(es)")

            # Effective removal of selected milestone tags
            for name in sel:
                # If the tag being removed is tagged in the repository, it is removed from the HEAD revision
                v = model.Tag(self.env, name, db=db)
                if v.tag_url:
                    unix_cmd_list = [util.SVN_TEMPLATE_CMD % {'subcommand': 'delete -m "%s" "%s"' % (
                        _('Removal of tag %(tag)s (on behalf of %(user)s)', tag=name, user=req.authname),
                        util.get_repo_url(self.env, util.get_url(v.tag_url)))}]
                    retcode, lines = util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())
                    if retcode != 0:
                        message = tag.p("Removal of ", tag.em("milestone tag(s)"), " in the repository has failed.", class_="message")
                        for line in lines:
                            message(tag.p(line))
                        raise TracError(message)
                    else:
                        # If the tag being destroyed is in status Accepted, the associated milestone is no more completed
                        if v.status == 'Accepted':
                            w = Milestone(self.env, name=v.tagged_item, db=db)
                            w.completed = 0
                            w.update(db=db)
                            # Effective update of the database
                            db.commit()
                v.delete(db=db)
                # If the tag being destroyed is baselined, the baseline is also removed
                for vv in [vv for vv in model.BaselineItem.select(self.env, ['baselined_tag="' + name + '"'], db=db)]:
                    vv.delete(db=db)
                # If the tag being removed is the last of the kind...
                remaining_milestone_tags = [w for w in model.Tag.select(self.env, ['tagged_item="' + v.tagged_item + '"'], db=db, tag_type=self.page_type)]
                if len(remaining_milestone_tags) == 0:
                    try:
                        ww = Milestone(self.env, name=v.tagged_item, db=db)
                        ww.delete(author=req.authname, db=db)
                    except ResourceNotFound:
                        pass
                # Effective update of the database
                db.commit()

            # if the milestone table turns empty, the 'Dummy' milestone is added
            remaining_milestones = [x for x in Milestone.select(self.env, db=db)]
            if len(remaining_milestones) == 0:
                d = Milestone(self.env, db=db)
                d.name = 'Dummy'
                # d.due not used (this is a dummy milestone)
                # d.completed not used (this is a dummy milestone)
                d.description = 'This milestone will be automatically removed when you create your first milestone tag'
                d.insert(db=db)
            # Effective update of the database
            db.commit()

            # custom field milestonetag options are updated
            milestonetags = "|".join([x.name for x in
                                      model.Tag.select(self.env,
                                                       db=db,
                                                       tag_type=self.page_type)])
            # field may be empty
            if milestonetags:
                milestonetags = "|" + milestonetags

            self.env.config.set('ticket-custom',
                                'milestonetag.options',
                                milestonetags)

            try:
                self.env.config.save()
            except Exception:
                e = sys.exc_info()[1]
                try:
                    self.log.error('Error writing to trac.ini: %s',
                                exception_to_unicode(e))
                    add_warning(req, _('Error writing to trac.ini, '
                                    'make sure it is writable by '
                                    'the web server. Your change '
                                    'has not been saved.'))
                finally:
                    del e

            add_notice(req, _("The selected milestone tags have been removed."))
            req.redirect(req.href.admin(cat, page,
                                        filter_value=data['filter_value'],
                                        sort_including=req.args.get('sort_including'),
                                        asc_including=req.args.get('asc_including'),
                                        sort_included=req.args.get('sort_included'),
                                        ScrollX=req.args.get('ScrollX'),
                                        ScrollY=req.args.get('ScrollY'),
                                        selected_item=req.args.get('selected_item')))

        # Tag Name filter:
        elif req.args.get('update'):
            req.redirect(req.href.admin(cat, page, filter_value=data['filter_value'], sort_including=req.args.get('sort_including'), asc_including=req.args.get('asc_including'), sort_included=req.args.get('sort_included'), ScrollX=req.args.get('ScrollX'), ScrollY=req.args.get('ScrollY'), selected_item=req.args.get('selected_item'), skill=req.args.get('skill'), from_tag=model.simplify_whitespace(req.args.get('from_tag')), change_type=req.args.get('change_type'), review=req.args.get('review'), add_review='false'))

    def _render_main_GET(self, req, cat, page, milestone_tag, data, db):
        if u'RF' in data['ticket_types']:
            # View RFs
            prf_report = self.env.config.get('artusplugin', 'PRF_report')
            add_ctxtnav(req, _('View RFs'), href='%s/report/%s?SKILL=%s&TAG=%s' % (data['base_path'], prf_report, '%', '%'))

        if u'EFR' in data['ticket_types']:
            # View EFRs
            add_ctxtnav(req, _('View EFRs'), href='%s/query?group=document&max=200&order=severity&col=id&col=summary&col=status&col=owner&col=milestone&col=severity&col=blockedby&col=keywords&col=time&type=EFR' % data['base_path'])

        # View ECRs
        add_ctxtnav(req, _('View ECRs'), href='%s/query?group=document&max=200&order=milestone&col=id&col=summary&col=status&col=owner&col=milestone&col=resolution&col=company&col=ecrtype&col=blocking&col=blockedby&col=keywords&col=time&type=ECR' % data['base_path'])

        # View milestones
        if 'MILESTONE_TAG_VIEW' in data['perm']:
            add_ctxtnav(req, _('View Milestones'), href='%s/admin/tags_mgmt/milestones' % data['base_path'])

        # Header used for sorting 'included' type table
        if req.args.get('sort_included') and req.args.get('asc_included'):
            data['sort_included'] = req.args.get('sort_included')
            data['asc_included'] = req.args.get('asc_included')
        else:
            data['sort_included'] = 'name'
            data['asc_included'] = '1'

        # Ordering term for SQL request
        ordering_term_included = data['sort_included']
        if data['asc_included'] == '1':
            ordering_term_included += ' ASC'
        else:
            ordering_term_included += ' DESC'

        # List of all Milestone tags
        milestone_tags = [milestone_tag for milestone_tag in model.Tag.select(self.env, ['name LIKE "%' + data['filter_value'] + '%"'], ordering_term=ordering_term_included, db=db, tag_type=self.page_type)]
        data['milestone_tags'] = milestone_tags
        data['milestone_tags_nb'] = len(milestone_tags)

        # skill list
        skills = OrderedDict()
        skill = None
        for elt in self.config['artusplugin'].getlist('conf_mgmt.skills',
                                                      '.SYS', '|'):
            [repos, skill] = elt.split('.')
            skills[skill] = repos

        data['skills'] = skills

        # Selected skill
        if req.args.get('skill'):
            data['skill'] = req.args.get('skill')
        elif skill:
            data['skill'] = self.env.config.get('artusplugin', 'default_skill', 'SYS')
        else:
            data['skill'] = None

        # Skill label
        data['skill_label'] = self.env.config.get('ticket-custom', 'skill.label', 'Skill')

        # From Tag list
        if data['skill']:
            from_tags = [v for v in model.Tag.select(self.env, ['tracked_item="%s"' % data['skill']], ordering_term='tracked_item, tagged_item, status', db=db, tag_type=self.page_type)]
            if from_tags:
                data['from_tags'] = util.group_by(from_tags, ('tagged_item', False), ('status', True))
            else:
                data['from_tags'] = None
        else:
            data['from_tags'] = None

        # From Tag selected value
        if req.args.get('from_tag'):
            data['from_tag'] = req.args.get('from_tag')
        elif data['from_tags']:
            data['from_tag'] = data['from_tags'][-1][-1][-1].name
        else:
            data['from_tag'] = None

        # WARNING: All record fields may be None
        from_tag = model.Tag(self.env, data['from_tag'], db=db)

        # Change Type list
        if data['from_tag'] is None:
            data['change_types'] = ['Creation']
        else:
            if from_tag.status == 'Accepted':
                data['change_types'] = ['Review']
            else:
                data['change_types'] = ['Review', 'Status', 'Status Index']

        # Change Type selected value
        if req.args.get('change_type'):
            data['change_type'] = req.args.get('change_type')
        elif data['change_types']:
            data['change_type'] = data['change_types'][-1]
        else:
            data['change_type'] = None

        # Reviews list
        add_review = None

        if data['skill']:
            reviews = [v.review
                       for v in model.Tag.select(self.env,
                                                 ['tracked_item = "%s"' % data['skill']],
                                                 db=db,
                                                 tag_type=self.page_type)]
            prefix = '%s_%s_' % (data['program_name'], data['skill'])
            reviews += [v.name[len(prefix):]
                        for v in Milestone.select(self.env,
                                                  include_completed=False,
                                                  db=db)
                        if v.name.startswith(prefix)]
            if reviews:
                # Unicity
                seen = set()
                reviews = [revw
                           for revw in reviews
                           if revw not in seen and
                           not seen.add(revw)]
                reviews.sort()
            elif req.args.get('review'):
                add_review = False
            else:
                add_review = True

            if data['change_type'] == 'Review':
                reviews = [revw
                           for revw in reviews
                           if revw != from_tag.review]
                if reviews:
                    add_review = False
                else:
                    add_review = True

            if req.args.get('add_review') == 'false':
                add_review = False
            elif req.args.get('add_review') == 'true':
                add_review = True

        if add_review is None:
            data['add_review'] = False
        else:
            data['add_review'] = add_review

        if data['skill']:
            data['reviews'] = reviews
            if req.args.get('review') and req.args.get('review') not in reviews:
                data['reviews'].append(req.args.get('review'))
            if len(data['reviews']) == 0:
                data['reviews'] = None
        else:
            data['reviews'] = None

        # Selected review
        if data['change_type'] == 'Creation' or data['change_type'] == 'Review':
            if data['reviews'] is not None:
                if req.args.get('review'):
                    data['review'] = req.args.get('review')
                elif req.args.get('add_review'):
                    data['review'] = None
                else:
                    data['review'] = data['reviews'][-1]
            else:
                data['review'] = None
        else:
            data['review'] = from_tag.review

        # Milestone name
        if data['review']:
            data['milestone_name'] = '%(program_name)s_%(skill)s_%(review)s' % {
                'program_name': data['program_name'],
                'skill': data['skill'],
                'review': data['review']}
        else:
            data['milestone_name'] = None

        # Status
        if data['change_type'] is None:
            data['status_list'] = None
            data['status'] = None
        elif data['change_type'] != 'Status Index':
            # Status list
            status_list = ['Prepared', 'Reviewed', 'Accepted']

            # Status is changed so it can't be the same as that of From Milestone
            if data['change_type'] == 'Status':
                status_list = [st
                               for st in status_list
                               if st != from_tag.status]

            # Status 'Accepted' is not in the list if it's already an existing miletone
            if 'Accepted' in status_list:
                if [v.name
                    for v in model.Tag.select(self.env,
                                              ['tracked_item="%s"' % data['skill'],
                                               'review="%s"' % data['review'],
                                               'status="%s"' % 'Accepted'],
                                              db=db,
                                              tag_type=self.page_type)]:
                    status_list = [st
                                   for st in status_list
                                   if st != 'Accepted']

            # Status 'Accepted' is not in the list if there are no Reviewed status
            if 'Accepted' in status_list:
                if not [v.name
                        for v in model.Tag.select(self.env,
                                                  ['tracked_item="%s"' % data['skill'],
                                                   'review="%s"' % data['review'],
                                                   'status="%s"' % 'Reviewed'],
                                                  db=db,
                                                  tag_type=self.page_type)]:
                    status_list = [st
                                   for st in status_list
                                   if st != 'Accepted']

            # Final list
            data['status_list'] = status_list

            # Status selected value
            if req.args.get('status'):
                data['status'] = req.args.get('status')
            else:
                data['status'] = data['status_list'][-1]
        else:
            # Status resulting from Change Type and From Milestone
            data['status'] = from_tag.status

        # Status Index resulting from Skill Name, From Milestone, Change Type and Status
        if data['review'] and data['status'] and data['status'] != 'Accepted':
            tg = [v.status_index
                  for v in model.Tag.select(self.env,
                                            ['tracked_item="%s"' % data['skill'],
                                             'review="%s"' % data['review'],
                                             'status="%s"' % data['status']],
                                            db=db,
                                            tag_type=self.page_type)]
            if len(tg) == 0:
                data['status_index'] = 1
            else:
                data['status_index'] = max(tg) + 1
        else:
            data['status_index'] = None

        # Milestone tag name
        if data['milestone_name']:
            if data['status']:
                data['tag_name'] = data['milestone_name'] + '.%(status)s' % {'status': data['status']}
                if data['status_index'] is not None:
                    data['tag_name'] += str(data['status_index'])
            else:
                data['tag_name'] = None
        else:
            data['tag_name'] = None


class MilestonesAdminPanel(TagsMgmt, MilestoneAdminPanel):
    """ Admin panels for Milestone Management. """

    page_type = MilestoneAdminPanel._type
    page_label = MilestoneAdminPanel._label

    # IPermissionRequestor method

    def get_permission_actions(self):
        """ Same permissions as MilestoneTagsAdminPanel - Not redefined """
        return []

    # MilestonesAdminPanel methods

    def _render_admin_panel(self, req, cat, page, milestone):
        return MilestoneAdminPanel._render_admin_panel(self, req, cat, page, milestone)


class VersionTagsAdminPanel(TagsMgmt):
    """ Admin panels for Version Tags Management. """

    page_type = 'version_tags'
    page_label = ('Version Tag', 'Version Tags')
    _actions = ['VERSION_TAG_VIEW',
                'VERSION_TAG_CREATE',
                'VERSION_TAG_MODIFY',
                'VERSION_TAG_APPLY',
                'VERSION_TAG_DELETE']
    _admin_privilege = 'VERSION_TAG_ADMIN'

    # IPermissionRequestor method

    def get_permission_actions(self):
        """ Definition of rights regarding version tags management
        VERSION_TAG_xxx : rights to view, create, modify, apply or delete a version tag

        E.g.:
                                authenticated      developer     authorized      admin
        VERSION_TAG_VIEW               X               X              X            X
        VERSION_TAG_CREATE                             X              X            X
        VERSION_TAG_MODIFY                             X              X            X
        VERSION_TAG_APPLY                              X(*)           X            X
        VERSION_TAG_DELETE                                            X(**)        X
        VERSION_TAG_ADMIN                                                          X

        (*) A non baselined version tag cannot be applied by developer if status is Released
            A baselined version tag cannot be applied by developer
        (**) A version tag can be deleted by authorized user only if not yet applied

        'applied' means 'tag created in the repository'

        """
        return self._actions + [(self._admin_privilege, self._actions)]

    # VersionTagsAdminPanel methods

    def _render_admin_panel(self, req, cat, page, version_tag):

        db = self.env.get_db_cnx()
        data = init_data(self, req)
        data['ticket_types'] = [tkt_type.name for tkt_type in Type.select(self.env)]

        if version_tag:
            self._render_detail_view(req, cat, page, version_tag, data, db)
        else:
            self._render_main_view(req, cat, page, version_tag, data, db)

        return 'version_tags.html', data

    def _render_detail_view(self, req, cat, page, version_tag, data, db):
        data['tg'] = model.Tag(self.env, version_tag, db=db)
        data['tag_url'] = self.tag_url_from_source_url(self.env,
                                                       data['tg'].name)
        if req.method == 'POST':
            self._render_detail_POST(req, cat, page, version_tag, data, db)

        self._render_detail_GET(req, cat, page, version_tag, data, db)

    def _render_detail_POST(self, req, cat, page, version_tag, data, db):
        # Version Tag Reqtify project and export script
        if req.args.get('reqtify_project'):

            upload = req.args['reqtify_project_file']
            filename = util.upload_filename(upload, 'rqtf')
            if not filename:
                raise TracError(_('No file uploaded'))
            filepath = '%s/%s' % (REQTIFY_PROJECT_DIRECTORY, filename)
            rqtf = ReqtifyProject(self.env,
                                  upload.file,
                                  filepath)
            # Get documents described in the Reqtify project
            document_pattern = r"\A(" + data['program_name'] + \
                r"_(?:%s)(?:_(?:[^\W_]|-)+)?_(?:(?:[^\W_]|-)+))" \
                % self.env.config.get('ticket-custom', 'skill.options')
            reqtify_docs = rqtf.read(document_pattern)
            # Get baseline docs
            baseline_docs = {}
            seen_doc_versions = get_docs_from_including_tag(self.env, version_tag, set(), set())[0]
            # list is sorted automatically by each tuple's first element
            ordered_doc_versions = sorted(seen_doc_versions)
            # Group document versions by documents (tracked_item)
            # As document versions are sorted,
            # only the most recent one will be kept in the end
            for doc_version, subpath in ordered_doc_versions:
                doc_tag = data['get_tag'](doc_version)
                tracked_item = doc_tag.tracked_item
                tag_url = data['get_url'](doc_tag.tag_url)
                baseline_docs[tracked_item] = (doc_version, subpath, tag_url)
            # Update Reqtify doc paths
            rexp = '.*?%s(?:/(?:Draft|Proposed|Released)/(%s(?:_\d+\.\d+\.(?:Draft|Proposed|Released)\d*)))?(.*)'
            # Writes back Reqtify project
            rqtf.write(rqtf.update(reqtify_docs, baseline_docs, rexp, version_tag))
            # Generates export script
            batchpath = '%s/%s.bat' % (REQTIFY_PROJECT_DIRECTORY, filename.split('.')[0])
            tag_url = data['get_tag'](version_tag).tag_url
            rqtf.export(version_tag, tag_url, baseline_docs, reqtify_docs, batchpath)
            # Packages the Reqtify project file
            zippath = '%s/%s.zip' % (REQTIFY_PROJECT_DIRECTORY, filename.split('.')[0])
            util.create_archive_file(self.env, [rqtf.filepath, batchpath], zippath)
            req.redirect(zippath.replace('/tmp/.', '%s://%s/' % (req.scheme, util.get_hostname(self.env))))

        # Version Tag Index
        elif req.args.get('listing'):
            if data['tg'].tag_url:
                target_url = util.get_url(util.get_repo_url(self.env, data['tg'].tag_url))
                filepath = generate_tag_index(self.env, version_tag, target_url, data['program_name'], req.authname)
                req.redirect(filepath.replace('/tmp/.', '%s://%s/' % (req.scheme, util.get_hostname(self.env))))
            else:
                raise TracError(tag.p("Only applied tags can be listed", class_="message"))

        # External references
        elif req.args.get('save_refs'):
            data['tg'].tag_refs = req.args.get('refs')
            data['tg'].update(db=db)
            db.commit()

        elif req.args.get('change') == 'Remove selected items':
            # Remove Tag from Baseline
            sel = req.args.get('sel')
            if not sel:
                raise TracError(_('No included tag selected'))
            if not isinstance(sel, list):
                sel = [sel]
            for name in sel:
                v = model.BaselineItem(self.env, (name, version_tag), db=db)
                v.delete(db=db)
            db.commit()
            req.redirect(req.href.admin(cat,
                                        page,
                                        version_tag,
                                        filter_value=data['filter_value'],
                                        sort_including=req.args.get('sort_including'),
                                        asc_including=req.args.get('asc_including'),
                                        sort_included=req.args.get('sort_included'),
                                        ScrollX=req.args.get('ScrollX'),
                                        ScrollY=req.args.get('ScrollY'),
                                        selected_item=req.args.get('selected_item'),
                                        included_tag=req.args.get('included_tag')))

        elif req.args.get('change') == 'Apply changes':
            # Save url or sub-paths
            if req.args.get('source_url'):
                data['tg'].source_url = req.args.get('source_url')
                data['tg'].update(db=db)
                db.commit()
                req.redirect(req.href.admin(cat, page, version_tag))
            elif req.args.get('subpath'):
                tag_names = req.args.get('tag_name')
                subpaths = req.args.get('subpath')
                if not util.my_type(tag_names) == list:
                    tag_names = [tag_names]
                    subpaths = [subpaths]
                for tag_name, subpath in zip(tag_names, subpaths):
                    # Removal of non breaking hyphens: \\u2011
                    v = model.BaselineItem(self.env, (tag_name, version_tag), db=db)
                    if v.subpath != subpath:
                        v.subpath = subpath
                        v.update(db=db)
                db.commit()
                req.redirect(req.href.admin(cat,
                                            page,
                                            version_tag,
                                            filter_value=data['filter_value'],
                                            sort_including=req.args.get('sort_including'),
                                            asc_including=req.args.get('asc_including'),
                                            sort_included=req.args.get('sort_included'),
                                            asc_included=req.args.get('asc_included'),
                                            ScrollX=req.args.get('ScrollX'),
                                            ScrollY=req.args.get('ScrollY')))

        # Apply filter
        elif req.args.get('update'):
            req.redirect(req.href.admin(cat, page, version_tag, filter_value=data['filter_value'], sort_including=req.args.get('sort_including'), asc_including=req.args.get('asc_including'), sort_included=req.args.get('sort_included'), asc_included=req.args.get('asc_included'), ScrollX=req.args.get('ScrollX'), ScrollY=req.args.get('ScrollY'), selected_item=req.args.get('selected_item'), included_tag=model.simplify_whitespace(req.args.get('included_tag'))))

        # Include Tag into Baseline
        elif req.args.get('add') and req.args.get('included_tag'):
            included_tag = model.simplify_whitespace(req.args.get('included_tag'))

            try:
                model.BaselineItem(self.env, (included_tag, version_tag), db=db)
            except ResourceNotFound:
                if req.args.get('replaced_tag'):
                    # 'replaced_tag' is removed from Baseline
                    v = model.BaselineItem(self.env, (req.args.get('replaced_tag'), version_tag), db=db)
                    v.delete(db=db)
                    db.commit()

                # 'included_tag' is included into Baseline
                v = model.BaselineItem(self.env, db=db)
                v.name = included_tag
                v.baselined_tag = version_tag
                v.author = req.authname
                vv = model.Tag(self.env, name=v.name, db=db)
                norm_incl_source_url = data['normalize'](data['get_url'](vv.source_url))
                norm_vers_source_url = data['normalize'](data['get_url'](data['tg'].source_url))
                if norm_incl_source_url.startswith(norm_vers_source_url):
                    # Included tag is organically included in baseline
                    v.subpath = norm_incl_source_url[len(norm_vers_source_url):norm_incl_source_url.rfind('/')]
                else:
                    # Included tag is external to the baseline
                    # It is inserted with only it's parent folder
                    v.subpath = '/' + norm_incl_source_url.split('/')[-2]
                v.insert(db=db)
                db.commit()
                req.redirect(req.href.admin(cat, page, version_tag, filter_value=data['filter_value'], sort_including=req.args.get('sort_including'), asc_including=req.args.get('asc_including'), sort_included=req.args.get('sort_included'), ScrollX=req.args.get('ScrollX'), ScrollY=req.args.get('ScrollY'), selected_item=included_tag, included_tag=included_tag))
            else:
                raise TracError(tag.p(tag.em("Tag "), tag.a("%s" % included_tag, href=req.href.admin(cat, page, included_tag)), " already included in ", tag.em("version tag "), tag.a("%s" % version_tag, href=req.href.admin(cat, page, version_tag)), class_="message"))

        # Apply Tag into the repository
        elif req.args.get('apply'):
            tickets = self._tickets_to_be_closed(data['tg'], version_tag, data['program_name'], db)
            if tickets:
                # Some (P)RF have to be closed
                message = tag.p(tag.em("Version tag "),
                                tag.a("%s" % version_tag,
                                      href=req.href.admin(
                                          cat,
                                          page,
                                          version_tag)),
                                " has not been applied.",
                                class_="message")
                message(tag.p("In order to keep the project "
                              "clean and under control, "
                              "the following P(RF) opened on "
                              "a previous status or "
                              "version of this document - "
                              "in this line of development - "
                              "have to be closed first:"))
                for (t_id, t_summary) in tickets:
                    message(tag.p(tag.a(
                        "#%s" % t_id,
                        href=req.href.ticket(t_id)),
                        ": %s" % t_summary))
                add_warning(req, message)
                req.redirect(req.href.admin(cat, page, version_tag))
            tag_data = {}
            tag_data['tag_name'] = data['tg'].name
            tag_data['comment'] = req.args.get('comment')
            tag_data['authname'] = req.authname
            tag_data['buildbot_progbase'] = "%s/%s" % (
                BuildBotModule.buildbot_projects,
                data['trac_env_name'])
            tag_data['tag_url'] = data['tag_url']
            VersionTagsAdminPanel.apply_tag(self.env, req.href, cat, page,
                                            tag_data, db)
            data['tg'] = model.Tag(self.env, version_tag, db=db)  # updated tg (post commit)
            add_notice(req, tag(_('The tag '),
                                tag.a('%s' % version_tag,
                                      href="%s" % req.href.admin(cat, page, version_tag)),
                                _(' has been applied in the repository as revision '),
                                tag.a('%s' % util.get_revision(data['tg'].tag_url),
                                      href="%s" % util.get_tracbrowserurl(
                                          self.env,
                                          util.get_url(data['tg'].tag_url))),
                                '.'))
            req.redirect(req.href.admin(cat,
                                        page,
                                        selected_item=version_tag,
                                        filter_value=version_tag))

        # Cancel
        elif req.args.get('cancel'):
            req.redirect(req.href.admin(cat, page, selected_item=version_tag, filter_value=version_tag))

    def _render_detail_GET(self, req, cat, page, version_tag, data, db):
        data['view'] = 'detail'
        data['version_tag'] = data['get_tag'](version_tag)
        data['tag_refs'] = data['version_tag'].tag_refs
        data['refs_syntax'] = ("**Syntax**:[[br]][[br]]"
                               "`<program id>:<version tag>`[[br]][[br]]"
                               "__Note__: If the tag is in this TRAC database, the program id  - %s - can be omitted, as follows:[[br]][[br]]"
                               "`<version tag>`") % data['program_name']

        if req.args.get('ci_source_url') and data['version_tag'].tag_url is None:
            # A revision, if present, signifies a will to create a version from an old revision, so it is kept as is
            data['source_url'] = req.args.get('ci_source_url')
        else:
            data['source_url'] = data['version_tag'].source_url

        if not data['tg'].component:
            data['component'] = False
        else:
            data['component'] = True

        # Skill
        default_skill = self.env.config.get('artusplugin', 'default_skill', 'SYS')
        tagged_item = data['tg'].tagged_item
        if util.skill_is_unmanaged(self.env, tagged_item):
            # Default value for unmanaged skills
            skill = default_skill
        else:
            # skill is extracted from document/component name
            regular_expression = r"\A" + data['program_name'] + r"_(%s)_" % self.env.config.get('ticket-custom', 'skill.options')
            m = re.search(regular_expression, tagged_item)
            if m:
                skill = m.group(1)
            else:
                skill = default_skill

        if 'RF' in data['ticket_types'] or 'PRF' in data['ticket_types']:
            # View associated (P)RFs
            prf_report = self.env.config.get('artusplugin', 'PRF_report')
            add_ctxtnav(req, _('View Associated (P)RFs'), href='%s/report/%s?SKILL=%s&VERSION=%s' % (data['base_path'], prf_report, skill, tagged_item))

        if u'EFR' in data['ticket_types']:
            # View associated EFRs
            if data['tg'].status == 'Released':
                add_ctxtnav(req, _('View Associated EFRs'), href='%s/query?group=resolution&max=200&order=severity&col=id&col=summary&col=status&col=owner&col=milestone&col=severity&col=blockedby&col=keywords&col=time&type=EFR&document=%s' % (data['base_path'], data['version_tag'].name))

        if u'ECR' in data['ticket_types']:
            # View associated ECRs
            if data['tg'].status == 'Released':
                add_ctxtnav(req, _('View Associated ECRs'), href='%s/query?group=milestone&max=200&order=resolution&col=id&col=summary&col=status&col=owner&col=milestone&col=resolution&col=company&col=ecrtype&col=blocking&col=blockedby&col=keywords&col=time&type=ECR&document=%s' % (data['base_path'], data['version_tag'].name))

        # View associated version
        if 'VERSION_TAG_VIEW' in data['perm']:
            add_ctxtnav(req, _('View Associated Version'), href='%s/admin/tags_mgmt/versions/%s' % (data['base_path'], data['version_tag'].tagged_item))

        # View associated document / component
        if 'VERSION_TAG_VIEW' in data['perm']:
            if not data['tg'].name.startswith('ECM_'):
                try:
                    model.Document(self.env, data['tg'].tracked_item)
                    add_ctxtnav(req, _('View Associated Document'), href='%s/admin/tags_mgmt/documents/%s' % (data['base_path'], data['version_tag'].tracked_item))
                except ResourceNotFound:
                    add_ctxtnav(req, _('View Associated Component'), href='%s/admin/tags_mgmt/components/%s' % (data['base_path'], data['version_tag'].tracked_item))

        # View included documents
        if 'VERSION_TAG_VIEW' in data['perm']:
            if not data['tg'].name.startswith('ECM_'):
                try:
                    model.Document(self.env, data['tg'].tracked_item)
                except ResourceNotFound:
                    add_ctxtnav(req, _('View Included Documents'), href='%s/admin/tags_mgmt/documents?filter_value=&select_tag=%s&pdf_packaging=true' % (data['base_path'], data['version_tag'].name))

        if data['tg'].baselined == 0:
            data['baselined'] = False
        else:
            data['baselined'] = True

        if data['tg'].buildbot == 0:
            data['buildbot'] = False
        else:
            data['buildbot'] = True

        if data['buildbot']:
            # List of possible source CSCIs associated with current source code: tagged baseline with same tracked_item, standard, edition and revision
            source_tags = [v for v in model.Tag.select(self.env,
                                                       ['tracked_item = "%s"' % data['tg'].tracked_item[:-len('_EOC')],
                                                        'standard = "%s"' % data['tg'].standard,
                                                        'edition = "%s"' % data['tg'].edition,
                                                        'revision = "%s"' % data['tg'].revision,
                                                        'tag_url IS NOT NULL', 'baselined = 1'],
                                                       db=db, tag_type=self.page_type)]

            # The source CSCIs list is filtered out further, a 'makefile' or 'Makefile' has to be found in one of the baseline documents (the build source document)
            buildable_cscis = {}
            for source_tag in source_tags:
                # Elements of baseline
                child_tags = [data['get_tag'](v.name) for v in model.BaselineItem.select(self.env, ['baselined_tag="' + source_tag.name + '"'], db=db)]
                # Documents (documents flagged as NOT builder are filtered out)
                not_builder_docs = [doc['name'] for doc in model.Document.select(self.env, ['builder=0'], db=db)]
                doc_tags = [doc_tg for doc_tg in child_tags if doc_tg.review is None and doc_tg.component == 0 and doc_tg.baselined == 0 and doc_tg.buildbot == 0 and doc_tg.tracked_item not in not_builder_docs]
                # Buildable source document - for now there should be one only
                buildable_tags = []
                for doc_tag in doc_tags:
                    for filenode in browse_for_files_in_repo(self, req, doc_tag.tag_url):
                        if filenode.name in ['Makefile', 'makefile']:
                            makefile_subpath = filenode.path[len(util.get_url(doc_tag.tag_url)):]
                            makefile_url = util.get_repo_url(self.env, doc_tag.tag_url).replace('/tags/versions', '').rsplit('/', 2)[0] + makefile_subpath
                            if 'output_dirs' in filenode.get_content().read():
                                # We need a makefile appropriate for building
                                buildable_tags.append(makefile_url)
                # Valid tags have exactly one m(M)akefile
                if len(buildable_tags) == 1:
                    buildable_cscis[source_tag.name] = buildable_tags[0]

            csci_names = buildable_cscis.keys()
            csci_names.sort()

            # Default values
            data['prod_csci_name'] = None
            data['check_csci_name'] = None
            data['prod_csci_url'] = None
            data['check_csci_url'] = None
            data['prod_category'] = None
            data['check_category'] = None
            data['prod_makefile_url'] = None
            data['check_makefile_url'] = None
            data['prod_builder_name'] = None
            data['check_builder_name'] = None
            data['prod_build_no'] = None
            data['check_build_no'] = None
            data['prod_status_url'] = None
            data['check_status_url'] = None
            data['prod_eoc_url'] = None
            data['check_eoc_url'] = None

            if csci_names:

                # In check mode - tag applied - cscis should also include EOC
                if data['tg'].tag_url:
                    if data['tg'].status == 'Candidate':
                        # Candidates EOC are not included in cscis, Released ones are
                        code_released_status = self.env.config.get('artusplugin', 'code_released_status')
                        vtag = '%s%s' % (data['tg'].tagged_item, code_released_status)
                    else:
                        vtag = version_tag
                    data['csci_names'] = []
                    for csci_name in csci_names:
                        # Candidates source baselines are not checked
                        t = model.Tag(self.env, csci_name, db=db)
                        if t.status == 'Candidate':
                            continue
                        try:
                            model.BaselineItem(self.env, (vtag, csci_name), db=db)
                            data['csci_names'].append(csci_name)
                        except ResourceNotFound:
                            continue
                else:
                    data['csci_names'] = csci_names

                # Selected source CSCI name
                if 'csci_name' in req.args:
                    if data['tg'].tag_url is None:
                        data['prod_csci_name'] = req.args.get('csci_name')
                    else:
                        try:
                            data['prod_csci_name'] = Build(self.env, (data['tg'].builder, data['tg'].build_no), db=db).CSCI_tag
                            if data['csci_names']:
                                data['check_csci_name'] = req.args.get('csci_name')
                        except ResourceNotFound:
                            pass
                else:
                    if data['tg'].tag_url is None:
                        data['prod_csci_name'] = data['csci_names'][-1]
                    else:
                        try:
                            data['prod_csci_name'] = Build(self.env, (data['tg'].builder, data['tg'].build_no), db=db).CSCI_tag
                            if data['csci_names']:
                                if data['prod_csci_name'] in data['csci_names']:
                                    data['check_csci_name'] = data['prod_csci_name']
                                else:
                                    data['check_csci_name'] = data['csci_names'][-1]
                        except ResourceNotFound:
                            pass

                # Selected source CSCI url
                if data['prod_csci_name']:
                    data['prod_csci_url'] = req.href.admin(cat, page, data['prod_csci_name'])
                if data['check_csci_name']:
                    data['check_csci_url'] = req.href.admin(cat, page, data['check_csci_name'])

                # Category associated with the selected source CSCI
                regular_expression = r"\A" + data['program_name'] + r"_(%s)_(?:((?:\w|-)+)_)?(?:(?:\w|-)+)EOC\Z" % self.env.config.get('ticket-custom', 'skill.options')
                match = re.search(regular_expression, data['tg'].tracked_item)
                if match:
                    skill = match.group(1)
                    csci = match.group(2)

                    # category names and build pages
                    category_prefix = data['trac_env_name'] + '_' + skill
                    if csci:
                        category_prefix += '_' + csci
                    data['prod_category'] = category_prefix + '_prod'
                    data['check_category'] = category_prefix + '_check'
                    build_url = self.env.base_url + '/build'
                    if data['prod_csci_name'] and data['prod_csci_name'] in buildable_cscis:
                        data['prod_makefile_url'] = '%(build_url)s?build_type=prod&makefile_url=%(makefile_url)s&csci_name=%(csci_name)s&eoc_name=%(eoc_name)s&eoc_tag_url=%(eoc_tag_url)s' % {
                                                    'build_url': build_url,
                                                    'makefile_url': buildable_cscis[data['prod_csci_name']],
                                                    'csci_name': data['prod_csci_name'],
                                                    'eoc_name': data['version_tag'].name,
                                                    'eoc_tag_url': self.env.base_url + req.path_info}
                    if data['check_csci_name'] and data['check_csci_name'] in buildable_cscis:
                        data['check_makefile_url'] = '%(build_url)s?build_type=check&makefile_url=%(makefile_url)s&csci_name=%(csci_name)s&eoc_name=%(eoc_name)s&eoc_tag_url=%(eoc_tag_url)s' % {
                                                     'build_url': build_url, 'makefile_url': buildable_cscis[data['check_csci_name']], 'csci_name': data['check_csci_name'], 'eoc_name': data['version_tag'].name, 'eoc_tag_url': self.env.base_url + req.path_info}

                    # Build number and build status
                    if data['prod_csci_name']:
                        data['prod_builder_name'] = '%(component)s_%(build type)s' % {'component': data['get_tag'](data['prod_csci_name']).tracked_item, 'build type': 'prod'}
                        data['prod_builder_name'] = data['prod_builder_name'].replace(data['program_name'], data['trac_env_name'], 1)
                    if data['check_csci_name']:
                        data['check_builder_name'] = '%(component)s_%(build type)s' % {'component': data['get_tag'](data['check_csci_name']).tracked_item, 'build type': 'check'}
                        data['check_builder_name'] = data['check_builder_name'].replace(data['program_name'], data['trac_env_name'], 1)

                    if data['tg'].tag_url is None:
                        # Until tag is applied, the build source is a drop-down list, the build page is active and the build no is either the last build if associated with the selected build-source and the EOC or None
                        builds = [build for build in Build.select(self.env, ['builder="' + data['prod_builder_name'] + '"'], db=db)]
                        if builds and builds[-1].completed and builds[-1].CSCI_tag == data['prod_csci_name'] and builds[-1].EOC_tag == data['tg'].name:
                            data['tg'].build_no = builds[-1].build_no
                            data['tg'].source_url = builds[-1].build_path + '/' + data['tg'].tracked_item
                        else:
                            data['tg'].build_no = None
                            data['tg'].source_url = None
                        data['tg'].builder = data['prod_builder_name']
                        data['tg'].update(db=db)
                        db.commit()
                        if 'ci_source_url' not in req.args:
                            data['version_tag'] = data['tg']
                            data['source_url'] = data['tg'].source_url
                    else:
                        if data['check_csci_name']:
                            # After tag has been applied, the build no is either the last build if associated with the selected build-source and the EOC or None
                            builds = [build for build in Build.select(self.env, ['builder="' + data['check_builder_name'] + '"'], db=db)]
                            if builds and builds[-1].completed and builds[-1].CSCI_tag == data['check_csci_name'] and builds[-1].EOC_tag == data['tg'].name:
                                data['check_build_no'] = builds[-1].build_no
                            else:
                                data['check_build_no'] = None

                    data['prod_build_no'] = data['tg'].build_no

                    data['prod_status_url'] = '%(build_url)s?build_type=prod&status_url=%(status_url)s&category=%(category)s&csci_name=%(csci_name)s&builder_name=%(builder_name)s&username=%(username)s&eoc_name=%(eoc_name)s&eoc_tag_url=%(eoc_tag_url)s' % {
                                              'build_url': build_url,
                                              'status_url': "%(scheme)s://%(host)s/tracs/buildbot/builders/%(prod_builder_name)s/builds/%(prod_build_no)s" % {
                                                  'scheme': req.scheme,
                                                  'host': util.get_hostname(self.env),
                                                  'prod_builder_name': data['prod_builder_name'],
                                                  'prod_build_no': data['prod_build_no']},
                                              'category': data['prod_category'],
                                              'csci_name': data['prod_csci_name'],
                                              'builder_name': data['prod_builder_name'],
                                              'username': req.authname,
                                              'eoc_name': data['version_tag'].name,
                                              'eoc_tag_url': self.env.base_url + req.path_info}

                    data['check_status_url'] = '%(build_url)s?build_type=check&status_url=%(status_url)s&category=%(category)s&csci_name=%(csci_name)s&builder_name=%(builder_name)s&username=%(username)s&eoc_name=%(eoc_name)s&eoc_tag_url=%(eoc_tag_url)s' % {
                                               'build_url': build_url,
                                               'status_url': "%(scheme)s://%(host)s/tracs/buildbot/builders/%(check_builder_name)s/builds/%(check_build_no)s" % {
                                                   'scheme': req.scheme,
                                                   'host': util.get_hostname(self.env),
                                                   'check_builder_name': data['check_builder_name'],
                                                   'check_build_no': data['check_build_no']},
                                               'category': data['check_category'],
                                               'csci_name': data['check_csci_name'],
                                               'builder_name': data['check_builder_name'],
                                               'username': req.authname,
                                               'eoc_name': data['version_tag'].name,
                                               'eoc_tag_url': self.env.base_url + req.path_info}

                    # EOC browsing
                    if data['tg'].tag_url is None:
                        # Access to the buildbot workspace when not yet tagged (if feasible)
                        if data['tg'].source_url:
                            data['prod_eoc_url'] = '%(build_url)s?eoc_url=%(eoc_url)s' % {'build_url': build_url, 'eoc_url': "%(scheme)s://%(host)s/buildbot/%(trac_env_name)s%(source_url)s" % {'scheme': req.scheme, 'host': util.get_hostname(self.env), 'trac_env_name': data['trac_env_name'], 'source_url': data['tg'].source_url}}
                    else:
                        # Access to the subvsersion repository when tagged for prod build
                        data['prod_eoc_url'] = util.get_url(util.get_tracbrowserurl(self.env, data['tg'].tag_url))
                        # Access to the buildbot workspace for check build (if feasible)
                        if data['check_build_no'] is not None:
                            data['check_eoc_url'] = '%(build_url)s?eoc_url=%(eoc_url)s' % {'build_url': build_url, 'eoc_url': "%(scheme)s://%(host)s/buildbot/%(trac_env_name)s%(build_path)s/%(tracked_item)s" % {'scheme': req.scheme, 'host': util.get_hostname(self.env), 'trac_env_name': data['trac_env_name'], 'build_path': builds[-1].build_path, 'tracked_item': data['tg'].tracked_item}}
        else:
            if not data['baselined']:
                # Document last change revision - we want the last committed revision from the HEAD revision
                if data['source_url']:
                    data['last_changed_rev'] = util.get_last_path_rev_author(self.env, util.get_url(data['source_url']))[2]
                else:
                    data['last_changed_rev'] = None
            else:
                baseline = [v for v in model.BaselineItem.select(self.env, ['baselined_tag="' + version_tag + '"'], db=db)]

                # Baseline empty ?
                if len(baseline) == 0:
                    data['empty_baseline'] = True
                else:
                    data['empty_baseline'] = False

                # Filter analysis
                filter_value = data['filter_value']
                where_expr_list = []
                if filter_value.startswith('*'):
                    filter_value = filter_value[1:]
                    where_expr_list += ['baselined=1', 'buildbot=0']
                if filter_value.endswith(']'):
                    filter_value = filter_value[:-1]
                    where_expr_list += ['tracked_item LIKE "%' + filter_value + '"']
                else:
                    where_expr_list += ['name LIKE "%' + filter_value + '%"']

                # Only one tag for each tracked item
                already_included_tags = [v.name for v in model.BaselineItem.select(self.env, ['name LIKE "%' + filter_value + '%"', 'baselined_tag="' + version_tag + '"'], db=db)]

                # The tags to be included must have an associated tracked_item different from that of the baseline which I am currently defining
                my_tracked_item = data['tg'].tracked_item

                # List of tags susceptible to be included into the baseline - they must have been applied to be listed
                tags = [v for v in model.Tag.select(self.env, where_expr_list, db=db, tag_type=self.page_type) if v.name not in already_included_tags
                        and v.tracked_item != my_tracked_item and v.tag_url and not v.tracked_item.startswith('ECM_%s_' % data['program_name'])]

                branch_segregation_activated = True if self.env.config.get('artusplugin', 'branch_segregation_activated') == 'True' else False
                if branch_segregation_activated and data['source_url']:
                    branch = util.get_branch(data['source_url'])
                    tags = [v for v in tags if NamingRule.get_branch_from_tag(self.env, v.name) == branch]

                tags = get_sorted_tags_by_rev(tags)
                data['included_tags_nb'] = len(tags)

                if tags:
                    # Tags are grouped by 'tagged_item' and by 'status'
                    data['included_tags'] = util.group_by(tags, ('tagged_item', False), ('status', True))
                else:
                    data['included_tags'] = None

                if req.args.get('included_tag') and req.args.get('included_tag') in [v.name for v in tags]:
                    data['included_tag'] = req.args.get('included_tag')
                elif data['included_tags']:
                    data['included_tag'] = tags[-1].name
                else:
                    data['included_tag'] = None

                if data['included_tag']:

                    tracked_item = data['get_tag'](data['included_tag']).tracked_item

                    already_included_tracked_items = {}
                    for already_included_tag in already_included_tags:
                        already_included_tracked_item = data['get_tag'](already_included_tag).tracked_item
                        already_included_tracked_items[already_included_tracked_item] = already_included_tag

                    if tracked_item in already_included_tracked_items:
                        data['replaced_tag'] = already_included_tracked_items[tracked_item]
                    else:
                        data['replaced_tag'] = None

    def _render_main_view(self, req, cat, page, version_tag, data, db):
        if req.method == "POST":
            self._render_main_POST(req, cat, page, version_tag, data, db)

        self._render_main_GET(req, cat, page, version_tag, data, db)

    def _render_main_POST(self, req, cat, page, version_tag, data, db):
        # Create Tag
        if req.args.get('add') and req.args.get('tag_name'):
            tag_data = {}
            tag_data['tag_name'] = req.args.get('tag_name')
            tag_data['ci_name'] = req.args.get('ci_name')
            tag_data['authname'] = req.authname
            tag_data['modification'] = req.args.get('modification')
            tag_data['amendment'] = req.args.get('amendment')
            tag_data['standard'] = req.args.get('standard')
            tag_data['edition'] = req.args.get('edition')
            tag_data['revision'] = req.args.get('revision')
            tag_data['status'] = req.args.get('status')
            tag_data['status_index'] = req.args.get('status_index')
            tag_data['component'] = req.args.get('component')
            tag_data['source_url'] = req.args.get('source_url')
            tag_data['baselined'] = 'False' if req.args.get('baselined') in ('0', 'False') else 'True'
            tag_data['buildbot'] = 'False' if req.args.get('buildbot') in ('0', 'False') else 'True'
            tag_data['version_type'] = req.args.get('version_type')
            tag_data['from_tag'] = req.args.get('from_tag')
            tag_data['program_name'] = data['program_name']
            tag_data['ticket_id'] = None

            VersionTagsAdminPanel.create_tag(self.env, req.href, cat,
                                             page, tag_data, db)
            req.redirect(req.href.admin(cat, page, tag_data['tag_name']))

        # Remove Tag
        elif req.args.get('remove'):
            sel = req.args.get('sel')
            if not sel:
                raise TracError(_('No version tag selected'))
            if not isinstance(sel, list):
                sel = [sel]

            # The version tags can only be removed if each of them is not included in a baseline not removed itself
            blocking = []
            for name in sel:
                blocking += [(v.name, v.baselined_tag) for v in model.BaselineItem.select(self.env, ['name="' + name + '"'], db=db) if v.baselined_tag not in sel]
            if blocking:
                message = tag()
                for block in blocking:
                    w = model.Tag(self.env, block[1], db=db)
                    if w.tag_url is None:
                        tagged = '- not yet applied - '
                    else:
                        tagged = '- applied - '
                    if w.review is None:
                        including_page = VersionTagsAdminPanel.page_type
                    else:
                        including_page = MilestoneTagsAdminPanel.page_type
                    message(tag.p("Can't remove ", tag.em("included tag "), tag.a("%s" % block[0], href=req.href.admin(cat, page, block[0])), " because it is included in ", tagged, tag.em("baselined tag "), tag.a("%s." % block[1], href=req.href.admin(cat, including_page, block[1], selected_item="%s" % block[0]))))
                message(tag.p("Either remove the listed baselined tags or remove the problematic included tags from them (if the baselined tags are not yet applied).", class_="message"))
                raise TracError(message, "Cannot delete Tag(s) included in Baselines")

            # The version tags can only be removed if each of them is not used as baseline in one or more ticket(s)
            blocking = []
            for name in sel:
                cursor = db.cursor()
                cursor.execute("SELECT ticket FROM ticket_custom WHERE name='document' and value='%s'" % name)
                tkt_ids = [int(row[0]) for row in cursor]
                if tkt_ids:
                    blocking.append((name, len(tkt_ids)))
            if blocking:
                message = tag()
                for block in blocking:
                    message(tag.p("Can't remove ", tag.em("version tag "), tag.a("%s" % block[0], href=req.href.admin(cat, page, block[0])), " because it is used as a baseline tag in ", tag.a("%d " % block[1], href=req.href.query(group="status", document="%s" % block[0], order="priority", col=["id", "summary", "type", "document"])), "ticket(s)."))
                message(tag.p("If feasible, change the baseline tag(s) for the ticket(s) involved.", class_="message"))
                raise TracError(message, "Cannot delete tag(s) used as baseline(s) in one ore more ticket(s)")

            # The version tags can only be removed if each of them is not used as baseline for branching
            blocking = []
            for name in sel:
                blocking += [(branch.source_tag, branch.id) for branch in model.Branch.select(self.env, ['source_tag="' + name + '"'], db=db)]
            if blocking:
                message = tag()
                for block in blocking:
                    w = model.Branch(self.env, block[1], db=db)
                    if w.branch_url is None:
                        tagged = '- not yet applied - '
                    else:
                        tagged = '- applied - '
                    message(tag.p("Can't remove ", tag.em("version tag "), tag.a("%s" % block[0], href=req.href.admin(cat, page, block[0])), " because it is the source tag of ", tagged, tag.em("branch "), tag.a("B%s." % block[1], href=req.href.admin(cat, 'branches', block[1]))))
                message(tag.p("If feasible, remove the listed branches or change the source tag of them (if the branches are not yet applied).", class_="message"))
                raise TracError(message, "Cannot delete tag(s) used as source tags in one or more branch(es)")

            # Effective removal of selected version tags
            for name in sel:
                tag_data = {}
                tag_data['tag_name'] = name
                tag_data['authname'] = req.authname
                VersionTagsAdminPanel.remove_tag(self.env, req.href, cat,
                                                 page, tag_data, db)

            add_notice(req, _("The selected version tags have been removed."))
            req.redirect(req.href.admin(cat,
                                        page,
                                        filter_value=data['filter_value'],
                                        sort_including=req.args.get('sort_including'),
                                        asc_including=req.args.get('asc_including'),
                                        sort_included=req.args.get('sort_included'),
                                        ScrollX=req.args.get('ScrollX'),
                                        ScrollY=req.args.get('ScrollY'),
                                        selected_item=req.args.get('selected_item')))

        # Tag Name filter OR Separator/Version filter
        elif req.args.get('update'):
            separator = req.args.get('separator')
            if separator:
                separator = separator.replace(' ', '_')
            version = req.args.get('version')
            if version:
                version = version.replace(' ', '_')
            req.redirect(req.href.admin(cat, page, filter_value=data['filter_value'], sort_including=req.args.get('sort_including'), asc_including=req.args.get('asc_including'), sort_included=req.args.get('sort_included'), ScrollX=req.args.get('ScrollX'), ScrollY=req.args.get('ScrollY'), selected_item=req.args.get('selected_item'), ci_type=req.args.get('component') == 'True' and 'component' or 'document', ci_name=req.args.get('ci_name'), baselined=req.args.get('baselined'), ci_source_url=req.args.get('source_url'), ma=req.args.get('ma'), from_tag=model.simplify_whitespace(req.args.get('from_tag')), change_type=req.args.get('change_type'), separator=separator, version=version))

        # Doc compare
        elif req.args.get('compare'):
            if 'original' not in req.args or 'revised' not in req.args:
                raise TracError("Cannot compare: "
                                "You have not selected original and/or revised version(s)")
            ticket_module = TicketModule(self.env)
            o_tagname, o_tkid = req.args.get('original').split(',')
            o_tkt = Ticket(self.env, o_tkid)

            # Check original tag has been applied
            # through given DOC ticket and
            # original source file is a Word file
            o_changes = [change for change in
                         ticket_module.
                         rendered_changelog_entries(req, o_tkt)]
            found_o_tag = False
            o_source_file = None
            applied_regexp = '^Tag (.+) applied$'
            for o_change in reversed(o_changes):
                if not found_o_tag:
                    match = re.search(applied_regexp, o_change['comment'])
                    if match:
                        tg = match.group(1)
                        if tg == o_tagname:
                            found_o_tag = True
                else:
                    if 'sourcefile' in o_change['fields']:
                        o_source_file = o_change['fields']['sourcefile']['new']
                        break

            if not found_o_tag:
                raise TracError(tag.p("Cannot compare: Tag ", tag.a("%s" % o_tagname, href=req.href.admin(cat, page, o_tagname)),
                                      " was not applied through ticket ", tag.a("#%s" % o_tkid, href=req.href.ticket(o_tkid))))
            elif not o_source_file or not util.is_word_file(o_source_file):
                raise TracError("Cannot compare: "
                                "original source file (%s) "
                                "is not a Word file"
                                % o_source_file)

            r_tagname, r_tkid = req.args.get('revised').split(',')
            r_tkt = Ticket(self.env, r_tkid)
            if o_tagname != r_tagname:
                # Check revised tag has been applied
                # through given DOC ticket and
                # revised source file is a Word file
                r_changes = [change for change in
                             ticket_module.
                             rendered_changelog_entries(req, r_tkt)]
                found_r_tag = False
                r_source_file = None
                for r_change in reversed(r_changes):
                    if not found_r_tag:
                        match = re.search(applied_regexp, r_change['comment'])
                        if match:
                            tg = match.group(1)
                            if tg == r_tagname:
                                found_r_tag = True
                    else:
                        if 'sourcefile' in r_change['fields']:
                            r_source_file = r_change['fields']['sourcefile']['new']
                            break

                if not found_r_tag:
                    raise TracError(tag.p("Cannot compare: Tag ", tag.a("%s" % r_tagname, href=req.href.admin(cat, page, r_tagname)),
                                          " was not applied through ticket ", tag.a("#%s" % r_tkid, href=req.href.ticket(r_tkid))))
                elif not r_source_file or not util.is_word_file(r_source_file):
                    raise TracError("Cannot compare: "
                                    "revised source file (%s) "
                                    "is not a Word file"
                                    % r_source_file)
            else:
                # Compare tag with HEAD
                r_source_file = r_tkt['sourcefile']
                if not r_source_file or not util.is_word_file(r_source_file):
                    raise TracError("Cannot compare: "
                                    "revised source file (%s) "
                                    "is not a Word file"
                                    % r_source_file)
            o_vtg = model.Tag(self.env, name=o_tagname)
            o_url = util.get_url(util.get_repo_url(self.env,
                                                   o_vtg.source_url))
            o_export_url = unicode_quote('%s/%s' % (
                o_url,
                o_source_file), '/')

            o_export_url = '%s@%s' % (
                o_export_url,
                util.get_revision(o_vtg.source_url))

            r_vtg = model.Tag(self.env, name=r_tagname)
            r_url = util.get_url(util.get_repo_url(self.env,
                                                   r_vtg.source_url))
            # Compare with HEAD revision by default
            r_export_url = unicode_quote('%s/%s' % (
                r_url,
                r_source_file), '/')

            if o_tagname != r_tagname:
                r_export_url = '%s@%s' % (
                    r_export_url,
                    util.get_revision(r_vtg.source_url))

            # Launch diff
            clickonce_app_url = self.env.config.get('artusplugin', 'clickonce_app_url_MS_Office')
            diff_url = '%s?action=compare&url=%s&url=%s' % (clickonce_app_url,
                                                            o_export_url,
                                                            r_export_url)
            req.redirect(diff_url)

    def _render_main_GET(self, req, cat, page, version_tag, data, db):
        if u'RF' in data['ticket_types']:
            # View RFs
            prf_report = self.env.config.get('artusplugin', 'PRF_report')
            add_ctxtnav(req, _('View RFs'), href='%s/report/%s?SKILL=%s&TAG=%s' % (data['base_path'], prf_report, '%', '%'))

        if u'EFR' in data['ticket_types']:
            # View EFRs
            add_ctxtnav(req, _('View EFRs'), href='%s/query?group=document&max=200&order=severity&col=id&col=summary&col=status&col=owner&col=milestone&col=severity&col=blockedby&col=keywords&col=time&type=EFR' % data['base_path'])

        if u'ECR' in data['ticket_types']:
            # View ECRs
            add_ctxtnav(req, _('View ECRs'), href='%s/query?group=document&max=200&order=milestone&col=id&col=summary&col=status&col=owner&col=milestone&col=resolution&col=company&col=ecrtype&col=blocking&col=blockedby&col=keywords&col=time&type=ECR' % data['base_path'])

        # View versions
        if 'VERSION_TAG_VIEW' in data['perm']:
            add_ctxtnav(req, _('View Versions'), href='%s/admin/tags_mgmt/versions' % data['base_path'])

        # View documents
        if 'VERSION_TAG_VIEW' in data['perm']:
            add_ctxtnav(req, _('View Documents'), href='%s/admin/tags_mgmt/documents' % data['base_path'])

        # Filter analysis
        filter_value = data['filter_value']
        where_expr_list = []
        if filter_value.startswith('*'):
            filter_value = filter_value[1:]
            where_expr_list += ['baselined=1', 'buildbot=0']
        if filter_value.endswith(']'):
            filter_value = filter_value[:-1]
            where_expr_list += ['tracked_item LIKE "%' + filter_value + '"' + "ESCAPE '\\'"]
        else:
            where_expr_list += ['name LIKE "%' + filter_value + '%"' + " ESCAPE '\\'"]

        # Header used for sorting 'included' type table
        if req.args.get('sort_included') and req.args.get('asc_included'):
            data['sort_included'] = req.args.get('sort_included')
            data['asc_included'] = req.args.get('asc_included')
        else:
            data['sort_included'] = 'name'
            data['asc_included'] = '1'

        # Ordering term for SQL request
        if data['sort_included'] == 'branch':
            # Sorting by branch is complex, namely for EOC tags, so default sorting
            # Effective sorting will be performed afterwards
            ordering_term_included = 'name ASC'
        else:
            ordering_term_included = data['sort_included']
            if data['asc_included'] == '1':
                ordering_term_included += ' ASC'
            else:
                ordering_term_included += ' DESC'

        # List of all not filtered out Version Tags
        version_tags = [version_tag for version_tag in
                        model.Tag.select(self.env,
                                         where_expr_list,
                                         ordering_term=ordering_term_included,
                                         db=db,
                                         tag_type=self.page_type)]

        if data['sort_included'] == 'branch':
            class VersionTag(object):
                def __init__(self, env, tg):
                    self.tag = tg
                    self.branch_name = NamingRule.get_branch_from_tag(env, tg.name)
                    if self.branch_name == 'trunk':
                        self.branch_number = 0
                    elif self.branch_name.startswith('B'):
                        self.branch_number = int(self.branch_name[1:])
                    else:
                        self.branch_number = -1

            version_tags = [VersionTag(self.env, vtg) for vtg in version_tags]
            grouped_versiontags = util.group_by(version_tags, ('branch_number', True))

            version_tags = []
            if data['asc_included'] == '1':
                # Ascending
                for branch_group in grouped_versiontags:
                    if branch_group[-1].branch_name == 'trunk':
                        version_tags += [vtg.tag for vtg in branch_group]
                for branch_group in grouped_versiontags:
                    if branch_group[-1].branch_name != 'trunk' and branch_group[-1].branch_name != '?':
                        version_tags += [vtg.tag for vtg in branch_group]
                for branch_group in grouped_versiontags:
                    if branch_group[-1].branch_name == '?':
                        version_tags += [vtg.tag for vtg in branch_group]
            else:
                # Descending
                grouped_versiontags.reverse()
                for branch_group in grouped_versiontags:
                    if branch_group[-1].branch_name == '?':
                        vtags = [vtg.tag for vtg in branch_group]
                        vtags.reverse()
                        version_tags += vtags
                for branch_group in grouped_versiontags:
                    if branch_group[-1].branch_name != 'trunk' and branch_group[-1].branch_name != '?':
                        vtags = [vtg.tag for vtg in branch_group]
                        vtags.reverse()
                        version_tags += vtags
                for branch_group in grouped_versiontags:
                    if branch_group[-1].branch_name == 'trunk':
                        vtags = [vtg.tag for vtg in branch_group]
                        vtags.reverse()
                        version_tags += vtags

        data['version_tags'] = version_tags
        data['version_tags_nb'] = len(version_tags)

        # Known CI ?
        if 'ci_name' in req.args:
            tags = [v for v in model.Tag.select(self.env,
                    ['tracked_item="%s"' % req.args.get('ci_name')],
                    db=db, tag_type=self.page_type)]
            if tags:
                known_ci = True
            else:
                known_ci = False
        else:
            known_ci = False

        if known_ci:
            data['baselined'] = tags[-1].baselined
            data['ma'] = tags[-1].version_type
        else:
            data['baselined'] = False
            data['ma'] = False
        referer = req.get_header('Referer')
        if not referer or 'browser' not in referer:
            # Composite component
            if 'baselined' in req.args:
                if req.args.get('baselined') in ['True', 'true']:
                    data['baselined'] = True
                else:
                    data['baselined'] = False
            # Version Type: (S.)E.R or M.A ?
            if 'version_type' in req.args:
                data['ma'] = req.args.get('version_type') == 'ma'
            elif 'ma' in req.args:
                data['ma'] = req.args.get('ma') == 'True'
            if known_ci:
                if (data['baselined'] != tags[-1].baselined or
                    data['ma'] != tags[-1].version_type):
                    known_ci = False
                    del req.args['ci_name']

        # Buildbot
        if 'buildbot' in req.args and req.args.get('buildbot') in ['True', 'true']:
            data['buildbot'] = True
        else:
            data['buildbot'] = False

        # Version Type
        if data['ma']:
            version_type = 1
        else:
            version_type = 0

        data['branch_segregation_activated'] = True if self.env.config.get('artusplugin', 'branch_segregation_activated') == 'True' else False
        if data['branch_segregation_activated']:
            data['branch_segregation_first_branch'] = self.env.config.get('artusplugin', 'branch_segregation_first_branch', 'B1')
        else:
            data['branch_segregation_first_branch'] = None

        def unicity(tags, segregation):
            seen = set()
            for tg in tags:
                if segregation:
                    branch = NamingRule.get_branch_from_tag(self.env, tg.name)
                    if (tg.tracked_item, branch) not in seen and not seen.add((tg.tracked_item, branch)):
                        yield tg
                else:
                    if tg.tracked_item not in seen and not seen.add(tg.tracked_item):
                        yield tg

        class ConfigurationItem(object):
            def __init__(self, env, tg):
                self.ci_name = tg.tracked_item
                self.branch_name = NamingRule.get_branch_from_tag(env, tg.name)
                if self.branch_name == 'trunk':
                    self.branch_number = 0
                elif self.branch_name.startswith('B'):
                    self.branch_number = int(self.branch_name[1:])
                else:
                    self.branch_number = -1

        # Document CI list
        if 'VERSION_TAG_ADMIN' in data['perm']:
            doc_skills = []
        else:
            if 'DOC' in data['ticket_types']:
                doc_skills = self.env.config.get('artusplugin', 'doc_skills', '').split('|')
            else:
                doc_skills = []
        document_tags = list(unicity(
            [v for v in model.Tag.select(self.env, ['version_type=%s' % version_type, 'component=0'],
                                         db=db, tag_type=self.page_type)
            if not v.tracked_item.startswith('ECM_%s_' % data['program_name'])
            and not util.get_skill(self.env, v.tracked_item, data['program_name']) in doc_skills],
            data['branch_segregation_activated']))

        document_cis = [ConfigurationItem(self.env, d_tg) for d_tg in document_tags]
        grouped_configurationitems = util.group_by(document_cis, ('branch_number', True))

        document_cis_by_branch = OrderedDict()
        for branch_group in grouped_configurationitems:
            if branch_group[-1].branch_name == 'trunk':
                document_cis_by_branch[branch_group[-1].branch_name] = [ci.ci_name for ci in branch_group]
        for branch_group in grouped_configurationitems:
            if branch_group[-1].branch_name != 'trunk' and branch_group[-1].branch_name != '?':
                if not data['branch_segregation_activated'] or branch_group[-1].branch_number >= int(data['branch_segregation_first_branch'][1:]):
                    document_cis_by_branch[branch_group[-1].branch_name] = [ci.ci_name for ci in branch_group]
        for branch_group in grouped_configurationitems:
            if branch_group[-1].branch_name == '?':
                document_cis_by_branch[branch_group[-1].branch_name] = [ci.ci_name for ci in branch_group]

        # Component CI list
        # All non baselined components are listed: buildbot or non buildbot,
        # because it should be possible to create a non buildbot version tag
        # from a buildbot one and vice versa, in the first case when
        # releasing a candidate and in the last one when building formally
        # a first locally builded component
        baselined = 1 if data['baselined'] else 0

        component_tags = list(unicity(
            [v for v in model.Tag.select(self.env,
                           ['version_type=%s' % version_type,
                            'component=1',
                            'baselined=%s' % baselined],
                           db=db,
                           tag_type=self.page_type)],
            data['branch_segregation_activated']
            ))

        component_cis = [ConfigurationItem(self.env, c_tg) for c_tg in component_tags]
        grouped_configurationitems = util.group_by(component_cis, ('branch_number', True))

        component_cis_by_branch = OrderedDict()
        for branch_group in grouped_configurationitems:
            if branch_group[-1].branch_name == 'trunk':
                component_cis_by_branch[branch_group[-1].branch_name] = [ci.ci_name for ci in branch_group]
        for branch_group in grouped_configurationitems:
            if branch_group[-1].branch_name != 'trunk' and branch_group[-1].branch_name != '?':
                if not data['branch_segregation_activated'] or branch_group[-1].branch_number >= int(data['branch_segregation_first_branch'][1:]):
                    component_cis_by_branch[branch_group[-1].branch_name] = [ci.ci_name for ci in branch_group]
        for branch_group in grouped_configurationitems:
            if branch_group[-1].branch_name == '?':
                component_cis_by_branch[branch_group[-1].branch_name] = [ci.ci_name for ci in branch_group]

        if req.args.get('ci_source_url'):
            data['branch'] = util.get_branch(req.args.get('ci_source_url'))
        elif req.args.get('branch'):
            data['branch'] = req.args.get('branch')
        else:
            data['branch'] = 'trunk'

        # Analysis of CI: document or component ? Component by default because for documents, tags should now be set through them
        component = req.args.get('ci_type', 'component') == 'component'
        if req.args.get('ci_name'):
            if data['branch'] in document_cis_by_branch and req.args.get('ci_name') in document_cis_by_branch[data['branch']]:
                component = False
            elif data['branch'] in component_cis_by_branch and req.args.get('ci_name') in component_cis_by_branch[data['branch']]:
                component = True
        data['component'] = component

        data['browsed_ci'] = False

        if not data['component']:
            # Document CI list
            data['component_cis_by_branch'] = None
            data['document_cis_by_branch'] = document_cis_by_branch
            if req.args.get('ci_name'):
                if data['branch'] not in document_cis_by_branch:
                    document_cis_by_branch[data['branch']] = []
                if req.args.get('ci_name') not in document_cis_by_branch[data['branch']]:
                    data['document_cis_by_branch'][data['branch']].insert(-1, req.args.get('ci_name'))
                    data['browsed_ci'] = True
        else:
            # Component CI list
            data['document_cis_by_branch'] = None
            data['component_cis_by_branch'] = component_cis_by_branch
            if req.args.get('ci_name'):
                if data['branch'] not in component_cis_by_branch:
                    component_cis_by_branch[data['branch']] = []
                if req.args.get('ci_name') not in component_cis_by_branch[data['branch']]:
                    data['component_cis_by_branch'][data['branch']].insert(-1, req.args.get('ci_name'))
                    data['browsed_ci'] = True

        # Selected CI

        if req.args.get('ci_name'):
            data['ci_name'] = req.args.get('ci_name')
        elif data['component'] == False and data['document_cis_by_branch'] and data['branch'] in data['document_cis_by_branch']:
            data['ci_name'] = data['document_cis_by_branch'][data['branch']][-1]
        elif data['component'] == True and data['component_cis_by_branch'] and data['branch'] in data['component_cis_by_branch']:
            data['ci_name'] = data['component_cis_by_branch'][data['branch']][-1]
        else:
            data['ci_name'] = None

        # Is selected CI skill associated with DOC tickets ?
        data['doc_skill'] = util.get_doc_skill(self.env, data['ci_name'], data['program_name'])

        # Are our naming rules applied ?
        if data['ci_name']:
            if util.skill_is_unmanaged(self.env, data['ci_name']):
                data['unmanaged_skill'] = True
            else:
                data['unmanaged_skill'] = False
        else:
            data['unmanaged_skill'] = None

        # From Tag list
        if data['ci_name']:
            if 'VERSION_TAG_ADMIN' in data['perm']:
                excluded_tagged_items = []
            else:
                # Only from_tags with no associated DOC ticket will be listed
                if data['branch_segregation_activated']:
                    instr_pattern = '/trunk/' if data['branch'] == 'trunk' else '/branches/%s/' % data['branch']
                else:
                    instr_pattern = '/'
                cursor = db.cursor()
                sql = ("SELECT tc1.value||tc2.value "
                       "FROM ticket t,ticket_custom tc1,ticket_custom tc2,ticket_custom tc3 "
                       "where t.type='DOC' "
                       "and t.id=tc1.ticket "
                       "and t.id=tc2.ticket "
                       "and tc1.name='configurationitem' "
                       "and tc1.value='%s' "
                       "and tc2.name='versionsuffix' "
                       "AND tc1.ticket = tc3.ticket "
                       "AND tc3.name='sourceurl' "
                       "AND instr(tc3.value, '%s')=1"
                       % (data['ci_name'], instr_pattern)
                       )
                cursor.execute(sql)
                excluded_tagged_items = [row[0] for row in cursor]

            from_tags = [v for v in model.Tag.select(self.env,
                                                     ['tracked_item="%s"' % data['ci_name'],
                                                      'component=%d' % data['component'],
                                                      'baselined=%d' % data['baselined'],
                                                      'version_type=%d' % data['ma']],
                                                     db=db,
                                                     tag_type=self.page_type)
                         if v.tagged_item not in excluded_tagged_items]

            branch_segregation_activated = True if self.env.config.get('artusplugin', 'branch_segregation_activated') == 'True' else False
            if branch_segregation_activated:
                from_tags = [v for v in from_tags if NamingRule.get_branch_from_tag(self.env, v.name) == data['branch']]

            if from_tags:
                data['from_tags'] = util.group_by(from_tags, ('tagged_item', False), ('status', False))
            else:
                data['from_tags'] = None
        else:
            data['from_tags'] = None

        # From Tag selected value
        if req.args.get('from_tag'):
            data['from_tag'] = req.args.get('from_tag')
        elif data['from_tags']:
            data['from_tag'] = data['from_tags'][-1][-1][-1].name
        else:
            data['from_tag'] = None

        # WARNING: All record fields may be None before CI selection
        from_tag = model.Tag(self.env, data['from_tag'], db=db)

        if data['buildbot']:
            data['source_url'] = None
        else:
            if req.args.get('ci_source_url'):
                data['source_url'] = req.args.get('ci_source_url')
            else:
                data['source_url'] = from_tag.source_url
                if data['source_url']:
                    if (data['component'] and
                        not data['baselined'] and
                        from_tag.buildbot):
                        # Use case: buildbot candidate to release
                        data['source_url'] = from_tag.tag_url
                    else:
                        last_path_rev_author = util.get_last_path_rev_author(self.env, util.get_url(data['source_url']), util.get_revision(data['source_url']))
                        reponame = last_path_rev_author[0]
                        path = last_path_rev_author[1]
                        rev = last_path_rev_author[2]
                        if (path):
                            data['source_url'] = (reponame and '/' + reponame or '') + path + '?rev=' + rev
                        else:
                            data['source_url'] = None
                else:
                    data['source_url'] = None

        # Change Type list
        if data['from_tag'] is None:
            data['change_types'] = None
            if data['ci_name']:
                # Creation
                data['change_type'] = 'Creation'
            else:
                # Nothing
                data['change_type'] = None

        else:
            # New Version
            if data['unmanaged_skill']:
                # Our naming rules are NOT applied
                data['change_types'] = None
                data['change_type'] = 'Version'
            else:
                # Our naming rules are applied
                if data['component']:
                    # Component
                    if data['ma']:
                        data['change_types'] = ['Modification', 'Amendment']
                    else:
                        if data['branch_segregation_activated']:
                            data['change_types'] = ['Edition', 'Revision']
                        else:
                            data['change_types'] = ['Standard', 'Edition', 'Revision']
                    if not [v for v in model.Tag.select(self.env,
                                                        ['tagged_item="%s"' % from_tag.tagged_item,
                                                         'status="Released"'],
                                                        db=db, tag_type=self.page_type)]:
                        from_tag_released = False
                    else:
                        from_tag_released = True

                    if data['baselined'] or not from_tag_released or from_tag.status == "Patch":
                        data["change_types"] += ["Status", "Status Index"]
                    else:
                        data["change_types"] += ["Status"]
                else:
                    # Document
                    data['change_types'] = ['Edition', 'Revision']
                    try:
                        model.Tag(self.env, from_tag.tagged_item + '.Released', db=db)
                        from_tag_released = True
                    except ResourceNotFound:
                        from_tag_released = False
                    if not from_tag_released:
                        data['change_types'] += ['Status', 'Status Index']

        # Change Type selected value
        if req.args.get('change_type'):
            data['change_type'] = req.args.get('change_type')
        elif data['change_types']:
            data['change_type'] = data['change_types'][-1]
        elif data['change_type']:
            # Nothing to do
            pass
        else:
            data['change_type'] = None

        if data['unmanaged_skill']:
            # Our naming rules are NOT applied
            data['version_name'] = data['ci_name']
            if 'separator' in req.args:
                data['separator'] = req.args.get('separator')
            else:
                data['separator'] = '_V'
            if 'version' in req.args:
                data['version'] = req.args.get('version')
            else:
                data['version'] = None
            if data['version']:
                data['version_name'] += data['separator'] + data['version']
            data['tag_name'] = data['version_name']
        else:
            # Our naming rules are not applied
            if data['ma']:
                # Standard / Modification / amendment (it's a component)
                data['standards'] = None
                if data['branch_segregation_activated'] and data['branch'] != 'trunk' and data['branch'] >= data['branch_segregation_first_branch']:
                    data['standard'] = int(data['branch'][1:]) - int(data['branch_segregation_first_branch'][1:]) + 1
                else:
                    data['standard'] = None

                # Modification resulting from Change Type and From Version
                if from_tag.modification is not None:
                    if data['change_type'] == 'Modification':
                        data['modification'] = self._increment_modification(from_tag.modification)
                    else:
                        data['modification'] = from_tag.modification
                else:
                    if data['ci_name']:
                        data['modification'] = 'A00'
                    else:
                        data['modification'] = None

                # Amendment resulting from Change Type and From Version
                if data['change_type'] == 'Modification':
                    data['amendment'] = None
                elif data['change_type'] == 'Amendment':
                    if from_tag.amendment:
                        data['amendment'] = self._increment_amendment(from_tag.amendment)
                    else:
                        data['amendment'] = 'A'
                else:
                    data['amendment'] = from_tag.amendment

                # Version name
                if data['ci_name'] and data['modification'] is not None:
                    if data['branch_segregation_activated'] and data['branch'] != 'trunk' and data['branch'] >= data['branch_segregation_first_branch']:
                        data['version_name'] = data['ci_name'] + \
                        '_%(standard)d.%(modification)s' % {
                        'standard': data['standard'],
                        'modification': data['modification']}
                    else:
                        data['version_name'] = data['ci_name'] + \
                        '_%(modification)s' % {
                        'modification': data['modification']}
                    if data['amendment'] is not None:
                        data['version_name'] += '.%(amendment)s' % {
                            'amendment': data['amendment']}
                else:
                    data['version_name'] = None

            else:
                # Standard / Edition / Revision

                # Standard resulting from Change Type and From Version
                if data['component']:
                    data['standards'] = None
                    if from_tag.standard is not None:
                        # Previous version tag
                        if data['change_type'] == 'Standard':
                            data['standard'] = from_tag.standard + 1
                        else:
                            data['standard'] = from_tag.standard
                    else:
                        # No previous version tag
                        if data['ci_name']:
                            if data['branch_segregation_activated']:
                                if data['branch'] == 'trunk':
                                    standard = 1
                                elif data['branch'] == data['branch_segregation_first_branch']:
                                    standard = 0
                                else:
                                    standard = int(data['branch'][1:]) - int(data['branch_segregation_first_branch'][1:]) + 1
                                data['standards'] = [standard]
                            else:
                                if 'VERSION_TAG_ADMIN' in data['perm']:
                                    data['standards'] = range(0, 10)
                                else:
                                    data['standards'] = [0, 1]
                            if req.args.get('standard'):
                                data['standard'] = int(req.args.get('standard'))
                            else:
                                if data['branch_segregation_activated']:
                                    data['standard'] = data['standards'][0]
                                else:
                                    data['standard'] = 1
                        else:
                            data['standard'] = None
                else:
                    data['standards'] = None
                    if data['branch_segregation_activated'] and data['branch'] != 'trunk' and data['branch'] >= data['branch_segregation_first_branch']:
                        data['standard'] = int(data['branch'][1:]) - int(data['branch_segregation_first_branch'][1:]) + 1
                    else:
                        data['standard'] = None

                # Edition resulting from Change Type and From Version
                if from_tag.edition is not None:
                    if data['change_type'] == 'Standard':
                        data['edition'] = 0
                    elif data['change_type'] == 'Edition':
                        data['edition'] = from_tag.edition + 1
                    else:
                        data['edition'] = from_tag.edition
                else:
                    if data['ci_name']:
                        if data['component']:
                            data['edition'] = 0
                        else:
                            data['edition'] = 1
                    else:
                        data['edition'] = None

                # Revision resulting from Change Type and From Version
                if from_tag.revision is not None:
                    if data['change_type'] in ['Standard', 'Edition']:
                        data['revision'] = 0
                    elif data['change_type'] == 'Revision':
                        data['revision'] = from_tag.revision + 1
                    else:
                        data['revision'] = from_tag.revision
                else:
                    if data['ci_name']:
                        data['revision'] = 0
                    else:
                        data['revision'] = None

                # Version name
                if data['component']:
                    if data['ci_name'] and data['standard'] is not None and data['edition'] is not None and data['revision'] is not None:
                        data['version_name'] = data['ci_name'] + \
                            '_%(standard)02d.%(edition)02d.%(revision)02d' % {'standard': data['standard'],
                                                                              'edition': data['edition'],
                                                                              'revision': data['revision']}
                    else:
                        data['version_name'] = None
                else:
                    if data['ci_name'] and data['edition'] is not None and data['revision'] is not None:
                        if data['branch_segregation_activated'] and data['branch'] != 'trunk' and data['branch'] >= data['branch_segregation_first_branch']:
                            data['version_name'] = data['ci_name'] + \
                            '_%(standard)d.%(edition)d.%(revision)d' % {
                                'standard': data['standard'],
                                'edition': data['edition'],
                                'revision': data['revision']}
                        else:
                            data['version_name'] = data['ci_name'] + \
                                '_%(edition)d.%(revision)d' % {
                                    'edition': data['edition'],
                                    'revision': data['revision']}
                    else:
                        data['version_name'] = None

            # Status
            if data['change_type'] is None:

                data['status_list'] = None
                data['status'] = None

            elif data['change_type'] in ['Creation', 'Standard', 'Edition', 'Revision', 'Modification', 'Amendment']:

                if data['component']:
                    data['status_list'] = ['Engineering', 'Candidate']
                    data['status'] = req.args.get('status', 'Engineering')
                else:
                    data['status_list'] = ['Draft', 'Proposed']
                    data['status'] = req.args.get('status', 'Draft')
                if 'TRAC_ADMIN' in req.perm:
                    data['status_list'].append('Released')

            elif data['change_type'] == 'Status':

                # Status list
                data['status_list'] = []

                if data['component']:
                    if from_tag.standard == 0:
                        # No Release - this is a prototype. Therefore only Engineering or Patch
                        # Note: for a prototype, patches can be created without releasing
                        data['status_list'] += ['Patch', 'Engineering']
                    else:
                        if from_tag_released:
                            data["status_list"] += ["Patch"]
                        else:
                            data['status_list'] += ['Engineering', 'Candidate', 'Released']
                else:
                    if not from_tag_released:
                        data['status_list'] += ['Draft', 'Proposed', 'Released']

                # Change type == Status
                try:
                    data['status_list'].remove(from_tag.status)
                except ValueError:
                    pass

                # Status selected value
                if req.args.get('status'):
                    data['status'] = req.args.get('status')
                else:
                    if data['status_list']:
                        data['status'] = data['status_list'][-1]
                    else:
                        data['status'] = None
            else:
                # Status resulting from Change Type and From Version
                data['status'] = from_tag.status

            # Status Index resulting from Change Type, From Version and Status
            code_released_status = self.env.config.get('artusplugin',
                                                       'code_released_status')
            if data['ma']:
                if data['modification'] is not None:
                    if data['amendment'] is not None:
                        existing_tags = [v for v in model.Tag.select(self.env,
                                         ['tracked_item="%s"' % data['ci_name'],
                                          'modification="%s"' % data['modification'],
                                          'amendment="%s"' % data['amendment'],
                                          'status="%s"' % data['status'],
                                          'component=%d' % data['component'],
                                          'baselined=%d' % data['baselined'],
                                          'version_type=%d' % data['ma']],
                                         db=db, tag_type=self.page_type)
                                         if NamingRule.get_branch_from_tag(self.env, v.name) == data['branch']]
                    else:
                        existing_tags = [v for v in model.Tag.select(self.env,
                                         ['tracked_item="%s"' % data['ci_name'],
                                          'modification="%s"' % data['modification'],
                                          'amendment IS NULL',
                                          'status="%s"' % data['status'],
                                          'component=%d' % data['component'],
                                          'baselined=%d' % data['baselined'],
                                          'version_type=%d' % data['ma']],
                                         db=db, tag_type=self.page_type)
                                         if NamingRule.get_branch_from_tag(self.env, v.name) == data['branch']]
                    if existing_tags:
                        data['status_index'] = existing_tags[-1].status_index + 1
                    else:
                        if data['status'] == 'Released':
                            if code_released_status == 'R00' or data['baselined']:
                                data['status_index'] = 0
                            else:
                                data['status_index'] = None
                        else:
                            data['status_index'] = 1
                else:
                    data['status_index'] = None
            elif data['component']:
                if (data['standard'] is not None and
                    data['edition'] is not None and
                    data['revision'] is not None and
                    data['status']):
                    existing_tags = [v for v in model.Tag.select(self.env,
                                     ['tracked_item="%s"' % data['ci_name'],
                                      'standard="%d"' % data['standard'],
                                      'edition="%d"' % data['edition'],
                                      'revision="%d"' % data['revision'],
                                      'status="%s"' % data['status'],
                                      'component=%d' % data['component'],
                                      'baselined=%d' % data['baselined'],
                                      'version_type=%d' % data['ma']],
                                     db=db, tag_type=self.page_type)]
                    if existing_tags:
                        data['status_index'] = existing_tags[-1].status_index + 1
                    else:
                        if data['status'] in ['Engineering', 'Candidate', 'Patch']:
                            data['status_index'] = 1
                        else:  # Released
                            if code_released_status == 'R00' or data['baselined']:
                                data['status_index'] = 0
                            else:
                                data['status_index'] = None
                else:
                    data['status_index'] = None
            else:
                if (data['edition'] is not None and
                    data['revision'] is not None and
                    data['status'] in ['Draft', 'Proposed']):
                    existing_tags = [v for v in model.Tag.select(self.env,
                                     ['tracked_item="%s"' % data['ci_name'],
                                      'edition="%d"' % data['edition'],
                                      'revision="%d"' % data['revision'],
                                      'status="%s"' % data['status'],
                                      'component=%d' % data['component'],
                                      'baselined=%d' % data['baselined'],
                                      'version_type=%d' % data['ma']],
                                     db=db, tag_type=self.page_type)
                                     if NamingRule.get_branch_from_tag(self.env, v.name) == data['branch']]
                    if existing_tags:
                        data['status_index'] = existing_tags[-1].status_index + 1
                    else:
                        data['status_index'] = 1
                else:  # Released
                    data['status_index'] = None

            # Version tag name
            if data['version_name']:
                if data['component']:
                    if data['status']:
                        data['tag_name'] = data['version_name'] + '%(status)s' % {'status': data['status'][0]}
                        if data['status_index'] is not None:
                            data['tag_name'] = '%(tag_name)s%(status_index)02d' % {'tag_name': data['tag_name'], 'status_index': data['status_index']}
                    else:
                        data['tag_name'] = None
                else:
                    if data['status']:
                        data['tag_name'] = data['version_name'] + '.%(status)s' % {'status': data['status']}
                        if data['status_index'] is not None:
                            data['tag_name'] += str(data['status_index'])
                    else:
                        data['tag_name'] = None
            else:
                data['tag_name'] = None

    @staticmethod
    def create_tag(env, href, cat, page, tg_data, db):
        if tg_data['tag_name'] == tg_data['ci_name']:
            raise TracError(tag.p("The version tag name is the same "
                                  "as the CI name: the version is missing.",
                                  class_="message"))
        try:
            model.Tag(env, name=tg_data['tag_name'], db=db)
        except ResourceNotFound:
            v = model.Tag(env, db=db)
            v.name = tg_data['tag_name']
            # v.tagged_item - see below
            v.tracked_item = tg_data['ci_name']
            v.author = tg_data['authname']
            # v.review not used in this context
            if tg_data['modification']:
                if tg_data['standard']:
                    v.standard = tg_data['standard']
                v.modification = tg_data['modification']
                if tg_data['amendment']:
                    v.amendment = tg_data['amendment']
            else:
                if tg_data['standard']:
                    v.standard = tg_data['standard']
                if tg_data['edition']:
                    v.edition = tg_data['edition']
                if tg_data['revision']:
                    v.revision = tg_data['revision']
            if tg_data['status']:
                v.status = tg_data['status']
            if tg_data['status_index']:
                v.status_index = tg_data['status_index']
            if tg_data['component'] in ['True', 'true']:
                v.component = 1
            else:
                v.component = 0
            if v.component == 1:
                # component
                if util.skill_is_unmanaged(env, v.tracked_item):
                    # unmanaged skills, no status
                    # so tagged item == tag name
                    v.tagged_item = v.name
                else:
                    if v.modification:
                        # M.A.
                        if v.standard:
                            v.tagged_item = '%s_%s.%s' % (v.tracked_item, v.standard, v.modification)
                        else:
                            v.tagged_item = '%s_%s' % (v.tracked_item, v.modification)
                        if v.amendment:
                            v.tagged_item = '%s.%s' % (v.tagged_item, v.amendment)
                    else:
                        # S.E.R.
                        v.tagged_item = '%s_%02d.%02d.%02d' % (v.tracked_item,
                                                               int(v.standard),
                                                               int(v.edition),
                                                               int(v.revision))
            else:
                # document
                if util.skill_is_unmanaged(env, v.tracked_item):
                    # unmanaged skills, no status
                    # so tagged item == tag name
                    v.tagged_item = v.name
                else:
                    if v.standard and int(v.standard) != 0 and v.edition and v.revision:
                        # internal document (S.E.R.)
                        v.tagged_item = '%s_%d.%d.%d' % (v.tracked_item,
                                                         int(v.standard),
                                                         int(v.edition),
                                                         int(v.revision))
                    elif v.edition and v.revision:
                        # internal document (E.R.)
                        v.tagged_item = '%s_%d.%d' % (v.tracked_item,
                                                      int(v.edition),
                                                      int(v.revision))
                    else:
                        # ECM / FEE record
                        if tg_data['ticket_id']:
                            tkt = Ticket(env, tg_data['ticket_id'])
                            v.tagged_item = tkt['summary']
                        else:
                            raise TracError("tg_data['ticket_id'] not setup")
            if tg_data['source_url']:
                v.source_url = tg_data['source_url']
            # v.tag_url not known at this stage
            if tg_data['baselined'] in ['True', 'true']:
                v.baselined = 1
            else:
                v.baselined = 0
            if tg_data['buildbot'] in ['True', 'true']:
                v.buildbot = 1
            else:
                v.buildbot = 0
            if tg_data['version_type'] == 'ma':
                v.version_type = 1
            else:
                v.version_type = 0
            # If a baselined component, the external references are created
            # automatically if possible (from_tag)
            if v.baselined == 1:
                tg = model.Tag(env, name=tg_data['from_tag'], db=db)
                v.tag_refs = tg.tag_refs
            v.insert(db=db)
            # If a baselined component, the baseline is created automatically
            # if possible (from_tag)
            if v.baselined == 1:
                for from_tag in [from_tag for from_tag in
                                 model.BaselineItem.select(
                                     env,
                                     ['baselined_tag="' +
                                      model.simplify_whitespace(
                                          tg_data['from_tag']) +
                                      '"'], db=db)]:
                    vv = model.BaselineItem(env, db=db)
                    vv.name = from_tag.name
                    vv.baselined_tag = tg_data['tag_name']
                    vv.author = tg_data['authname']
                    vv.subpath = from_tag.subpath
                    vv.insert(db=db)
            # An entry is added in the version table if not already in it
            try:
                Version(env, name=v.tagged_item, db=db)
            except ResourceNotFound:
                w = Version(env, db=db)
                w.name = v.tagged_item
                # w.time will be set when the version is tagged
                # in the repository (if status is 'Released')
                # w.description will be input by hand
                w.insert(db=db)
                # Removes the 'Dummy' version if still there
                try:
                    x = Version(env, name='Dummy', db=db)
                    x.delete(db=db)
                except ResourceNotFound:
                    pass
            # An entry is added in the component table if a component
            # and not already in it
            if v.component == 1:
                try:
                    Component(env, name=v.tracked_item, db=db)
                except ResourceNotFound:
                    w = Component(env, db=db)
                    w.name = v.tracked_item
                    # w.owner will be input by hand
                    # w.description will be input by hand
                    w.insert(db=db)
                    # Removes the 'Dummy' component if still there
                    try:
                        x = Component(env, name='Dummy', db=db)
                        x.delete(db=db)
                    except ResourceNotFound:
                        pass
            # An entry is added in the document table if a document
            # and not already in it
            if v.component == 0 and not v.name.startswith('ECM_') and not v.name.startswith('FEE_'):
                try:
                    model.Document(env, v.tracked_item, db=db)
                except ResourceNotFound:
                    w = model.Document(env, db=db)
                    w['name'] = v.tracked_item
                    w['shortname'] = NamingRule.get_shortname(
                        env,
                        v.name,
                        tg_data['program_name'])
                    # w.description will be input by hand
                    if tg_data['ticket_id']:
                        DOC_tkt = Ticket(env, tg_data['ticket_id'])
                        cc_options = env.config.get('ticket-custom',
                                                    'controlcategory.options')
                        cc_values = [option.strip() for option in
                                     cc_options.split('|')]
                        w['controlcategory'] = cc_values.index(DOC_tkt['controlcategory'])
                    w.insert(db=db)
            db.commit()
        else:
            raise TracError(tag.p(tag.em("Version tag "),
                                  tag.a("%s" % tg_data['tag_name'],
                                        href=href.admin(
                                            cat,
                                            page,
                                            tg_data['tag_name'])),
                                  " already exists.", class_="message"))

    @staticmethod
    def apply_tag(env, href, cat, page, tg_data, db):
        vtg = model.Tag(env, name=tg_data['tag_name'])
        # comment string
        if tg_data['comment']:
            comment = tg_data['comment']
        else:
            comment = "tag %s" % vtg.name
        # The comment string probably comes from a PC (DOS) input
        comment = comment.replace('\r', '')
        comment += _(' (on behalf of %(user)s)', user=tg_data['authname'])
        # comment is stored in a temporary file because Popen want parameters
        # (commit message) to be coded in ASCII !
        msg_filename = '/tmp/%s.msg' % os.getpid()
        f = codecs.open(msg_filename, 'w', 'utf-8')
        f.write(comment)
        f.close()
        if vtg.buildbot == 1:
            # EOC component
            unix_cmd = (util.SVN_TEMPLATE_CMD % {
                'subcommand': 'import -F "%s"' % msg_filename} + '"' +
                tg_data['buildbot_progbase'] + vtg.source_url +
                '" "' + util.get_repo_url(env, tg_data['tag_url']) + '"')
        else:
            unix_cmd = util.SVNMUCC_TEMPLATE_CMD + '-F "%s" ' % msg_filename
            paths, dirs = util.analyse_url(env, tg_data['tag_url'])
            for dr in dirs:
                unix_cmd += 'mkdir "%s" ' % dr
            if vtg.baselined == 0:
                # Document or non-baselined component (except buildboted ones)
                    source_revision = util.get_revision(vtg.source_url)
                    if not source_revision:
                        source_revision = util.get_head_revision(
                            util.get_repository(env,
                                                vtg.source_url))
                    source_url = util.get_url(vtg.source_url)
                    unix_cmd += 'cp %s "%s" "%s" ' % (
                        source_revision,
                        util.get_repo_url(env, source_url),
                        paths[-1])
            else:
                # Baselined component
                repos = util.get_repository(env, tg_data['tag_url'])
                version_path = paths[-2] + '/' + tg_data['tag_name']
                unix_cmd += 'mkdir "%s" ' % version_path
                subdirs = []
                for v in [v for v in model.BaselineItem.select(env,
                          ['baselined_tag="' + tg_data['tag_name'] + '"'],
                          db=db)]:
                    vv = model.Tag(env, name=v.name, db=db)
                    if vv.tag_url is None:
                        raise TracError(tag.p(
                            tag.em("Version tag "),
                            tag.a("%s" % tg_data['tag_name'],
                                  href=href.admin(cat,
                                                  page,
                                                  tg_data['tag_name'])),
                            " could not be applied because ",
                            tag.em("included tag "),
                            tag.a("%s" % vv.name,
                                  href=href.admin(cat,
                                                  page,
                                                  vv.name)),
                            " has not been applied",
                            class_="message"))
                    else:
                        included_tag_url = vv.tag_url
                        included_tag_revision = util.get_revision(
                            included_tag_url)
                        if included_tag_revision == '':
                            raise TracError(tag.p(
                                tag.em("Version tag "),
                                tag.a("%s" % tg_data['tag_name'],
                                      href=href.admin(
                                          cat,
                                          page,
                                          tg_data['tag_name'])),
                                " could not be applied because ",
                                tag.em("tag "),
                                tag.a("%s" % vv.name,
                                      href=href.admin(cat, page, vv.name)),
                                " has an url without revision",
                                class_="message"))
                        included_tag_url = util.get_url(included_tag_url)
                        subpaths = []
                        subpath = v.subpath
                        while subpath:
                            subpaths.insert(0, subpath)
                            subpath = subpath[:subpath.rfind('/')]
                        tag_path = version_path
                        for subpath in subpaths:
                            tag_path = version_path + subpath
                            offset = paths[0].rfind('/')
                            if (not repos.has_node(tag_path[offset:], '') and
                               subpath not in subdirs):
                                unix_cmd += 'mkdir "%s" ' % tag_path
                                subdirs.append(subpath)
                        unix_cmd += 'cp %(rev)s "%(src_url)s" "%(tgt_url)s" ' % {
                            'rev': included_tag_revision,
                            'src_url': util.get_repo_url(env,
                                                         included_tag_url),
                            'tgt_url': tag_path + '/' + v.name}
        unix_cmd_list = [unix_cmd]
        retcode, lines = util.unix_cmd_apply(env, unix_cmd_list, util.lineno())
        # Temporary commit message file is removed
        try:
            os.remove(msg_filename)
        except os.error:
            pass
        # Result of UNIX commands
        if retcode != 0:
            message = tag.p("Applying of ",
                            tag.em("version tag "),
                            tag.a("%s" % vtg.name,
                                  href=href.admin(cat,
                                                  page,
                                                  vtg.name)),
                            " has failed.", class_="message")
            for line in lines:
                message(tag.p(line))
            raise TracError(message)
        else:
            match = re.match(r'^r(\d+) committed by trac at', lines[0])
            if not match:
                tag_revision = ''
                for line in lines:
                    if line.startswith(u'Révision '):
                        regular_expression = u'\ARévision (\d+) propagée\.\n\Z'
                        match = re.search(regular_expression, line)
                        if match:
                            tag_revision = match.group(1)
                        break
                if tag_revision == '':
                    message = tag.p("Applying of ",
                                    tag.em("version tag "),
                                    tag.a("%s" % vtg.name,
                                          href=href.admin(
                                              cat,
                                              page,
                                              vtg.name)),
                                    " has failed.", class_="message")
                    message(tag.p("Could not find revision associated with "
                                  "successful commit"))
                    raise TracError(message)
            else:
                tag_revision = match.group(1)

            vtg.tag_url = tg_data['tag_url'] + '?rev=' + tag_revision
            vtg.author = tg_data['authname']
            vtg.update(db=db)
            # The associated version is timed if applicable
            time_version = False
            if vtg.name.startswith('ECM_'):
                # Sending Date
                try:
                    int(vtg.status_index)
                except TypeError:
                    time_version = True
            else:
                # Release Date
                if vtg.status == 'Released':
                    time_version = True
            if time_version:
                w = Version(env, name=vtg.tagged_item, db=db)
                w.time = datetime.now(utc)
                w.update(db=db)
            db.commit()

    @staticmethod
    def remove_tag(env, href, cat, page, tg_data, db):
        # If the tag being removed is tagged in the repository,
        # it is removed from the HEAD revision
        name = tg_data['tag_name']
        v = model.Tag(env, name, db=db)
        if v.tag_url:
            unix_cmd_list = [util.SVN_TEMPLATE_CMD % {
                             'subcommand': 'delete -m "%s" "%s"' % (
                                 _('Removal of tag %(tag)s (on behalf of %(user)s)',
                                    tag=name, user=tg_data['authname']),
                                    util.get_url(util.get_repo_url(env, v.tag_url)))}]
            retcode, lines = util.unix_cmd_apply(env, unix_cmd_list, util.lineno())
            if retcode != 0:
                message = tag.p("Removal of ",
                                tag.em("version tag(s)"),
                                " in the repository has failed.",
                                class_="message")
                for line in lines:
                    message(tag.p(line))
                raise TracError(message)
        v.delete(db=db)

        # If the tag being destroyed is baselined, the baseline is also removed
        for vv in [vv for vv in model.BaselineItem.select(
                   env, ['baselined_tag="' + name + '"'], db=db)]:
            vv.delete(db=db)

        # If the tag being removed is the last of the kind...
        remaining_version_tags = [w for w in model.Tag.select(
                                  env,
                                  where_expr_list=['tagged_item="' + v.tagged_item + '"'],
                                  ordering_term='name ASC',
                                  db=db,
                                  tag_type=page)]
        if len(remaining_version_tags) == 0:
            try:
                # ...the associated version is removed from the Version table
                ww = Version(env, name=v.tagged_item, db=db)
                ww.delete(db=db)
            except ResourceNotFound:
                pass
            try:
                # ...the associated component is removed from the Component table
                ww = Component(env, name=v.tracked_item, db=db)
                ww.delete(db=db)
            except ResourceNotFound:
                pass
            try:
                ww = model.Document(env, name=v.tracked_item, db=db)
                # The document entry has been added either when creating a tag
                # or when creating a DOC ticket
                # It is removed if there is no associated DOC ticket
                cursor = db.cursor()
                cursor.execute("SELECT ticket FROM ticket_custom tc "
                               "WHERE tc.name='configurationitem' "
                               "AND tc.value='%s'" % ww['name'])
                row = cursor.fetchone()
                if not row:
                    # ...the associated document is removed from the Document table
                    ww.delete(db=db)
            except ResourceNotFound:
                pass
        else:
            remaining_released_version_tags = [w for w in remaining_version_tags if w.status == 'Released']
            if len(remaining_released_version_tags) == 0:
                # version time is reset
                version_time = 0
            else:
                # version time takes the time of most recent remaining released version tag
                tag_url = remaining_released_version_tags[-1].tag_url
                revision = util.get_revision(tag_url)
                changeset = util.get_repository(env, tag_url).get_changeset(revision)
                version_time = changeset.date
            try:
                ww = Version(env, name=v.tagged_item, db=db)
                ww.time = version_time
                ww.update(db)
            except ResourceNotFound:
                pass

        # Effective update of the database
        db.commit()

        # if the version table turns out to be empty, the 'Dummy' version is added
        remaining_versions = [x for x in Version.select(env, db=db)]
        if len(remaining_versions) == 0:
            d = Version(env, db=db)
            d.name = 'Dummy'
            # d.time not used (this is a dummy version)
            d.description = ('This version will be automatically removed '
                             'when you create your first version tag')
            d.insert(db=db)

        # if the component table turns out to be empty, the 'Dummy' component is added
        remaining_components = [x for x in Component.select(env, db=db)]
        if len(remaining_components) == 0:
            d = Component(env, db=db)
            d.name = 'Dummy'
            # d.owner not used (this is a dummy version)
            d.description = ('This component will be automatically removed '
                             'when you create your first component version tag')
            d.insert(db=db)

        # Effective update of the database
        db.commit()

    @staticmethod
    def tag_url_from_source_url(env, name):
        tg = model.Tag(env, name)
        source_url = util.get_url(tg.source_url)
        if source_url:
            if tg.buildbot:
                tag_url = '%s%s' % (
                    '/tags/versions',
                    source_url.replace('/prod/build', ''))
            else:
                if '/trunk/' in source_url:
                    tag_url = source_url.replace('/trunk/',
                                                 '/tags/versions/',
                                                 1)
                elif '/branches/' in source_url:
                    tag_url = source_url.replace('/branches/',
                                                 '/tags/versions/',
                                                 1)
                    # There are two types of branches:
                    # 1: /tags/versions/Bxx/... (usual case)
                    # 2: /tags/versions/.../<status>/<tag name>/... (legacy)
                    match = re.search('\A/tags/versions/(B\d+/).+\Z', tag_url)
                    if match:
                        tag_url = tag_url.replace(match.group(1), '')
                    else:
                        splitted_tag_url = tag_url.split('/')
                        for idx, elt in enumerate(splitted_tag_url):
                            if elt in ['Draft', 'Proposed', 'Released',
                                       'Engineering', 'Candidate',
                                       'Patch', 'Build']:
                                filtered_tag_url = '%s%s' % (
                                    splitted_tag_url[0:idx],
                                    splitted_tag_url[idx + 2:])
                                tag_url = '/'.join(filtered_tag_url)
                                break
                elif '/tags/' in source_url:
                    # Use case: create EOC Released from buildboted EOC Candidate
                    tag_url = source_url.rsplit('/', 2)[0]
                else:
                    raise TracError("An error has occured. "
                                    "Please contact your trac administrator.")
            if tg.status:
                # Internal
                tag_url += '/' + tg.status + '/' + name
            else:
                # External
                tag_url += '/' + name
        else:
            tag_url = None

        return tag_url

    def _tickets_to_be_closed(self, vtg, vers_tg, program_name, db):
        # For managed skills, check there is no open (P)RF
        # on the document on the same branch of development
        # for a previous version or a previous status if the
        # same version
        tickets = []

        # Document with managed skill
        if (vtg.review is None and
            not vtg.component and
            vtg.status != 'Draft' and
            not util.skill_is_unmanaged(self.env, vers_tg)):
            # List of all open P(RF) associated with the document
            # for a previous version or a previous status
            # if the same version
            cursor = db.cursor()
            cursor.execute("""
                SELECT DISTINCT t.id, t.summary, tc.value FROM
                ticket AS t, ticket_custom AS tc
                WHERE (t.type = 'RF' OR t.type = 'PRF')
                AND NOT t.status = 'closed'
                AND tc.ticket = t.id
                AND tc.name = 'document'
                AND tc.value LIKE '%s%%'
                AND tc.value < '%s'""" % (vtg.tracked_item,
                                          vers_tg))
            # Filter out P(RF) not on the same branch
            # of development
            regexp = r"\A([\w/]*?/(?:trunk|branches(?:/B\d+)?)/)"
            match = re.search(regexp, vtg.source_url)
            if match:
                branch = match.group(1)
                for (t_id, t_summary, tc_value) in cursor:
                    doc_tag = model.Tag(self.env, tc_value, db=db)
                    if doc_tag.source_url.startswith(branch):
                        tickets.append((t_id, t_summary))
        return tickets

    def _increment_modification(self, modification):
        if modification == 'Z99':
            return None
        regexp = r"\A([A-Z])(\d\d)\Z"
        match = re.search(regexp, modification)
        if match:
            letter = match.group(1)
            number = int(match.group(2))
            if number == 99:
                letter = chr(ord(letter) + 1)
                number = 0
            else:
                number += 1
            return '%s%02d' % (letter, number)
        else:
            return None

    def _increment_amendment(self, amendment):
        if amendment == 'Z':
            return None
        regexp = r"\A([A-Z])\Z"
        match = re.search(regexp, amendment)
        if match:
            return chr(ord(amendment) + 1)
        else:
            return None


class VersionsAdminPanel(TagsMgmt, VersionAdminPanel):
    """ Admin panels for Versions Management. """

    page_type = VersionAdminPanel._type
    page_label = VersionAdminPanel._label

    # IPermissionRequestor method

    def get_permission_actions(self):
        """ Same permissions as VersionTagsAdminPanel - Not redefined """
        return []

    # VersionsAdminPanel methods

    def _render_admin_panel(self, req, cat, page, version):
        return VersionAdminPanel._render_admin_panel(self, req, cat, page, version)


class TemplatesAdminPanel(CComponent):
    """ Admin panel for Templates Management. """

    implements(IAdminPanelProvider)

    """ Forms may be OOo forms or MSO forms. Associated ticket types are respectively:
        OOo forms: EFR / ECR / RF
        MSO forms: EFR / ECR / PRF / MOM / DOC / ECM / FEE

        OOo forms are defined at the project level:
         /trunk/00-PROJ/03-Change_Management/01-Templates/(EFR|ECR|RF)_<proj>_TEMPLATE

        MSO forms are defined at project or skill level as follows:
          EFR are:
            defined by default at project level: /srv/trac/<proj>/templates
            customizable at skill level : in the SYS CMP document
          ECR are:
            defined by default at project level: /srv/trac/<proj>/templates
            customizable at skill level : in the associated CMP document
          PRF are:
            defined by default at project level: /srv/trac/<proj>/templates
            customizable at skill level : in the associated VP document
          MOM are:
            defined by default at project level: /srv/trac/<proj>/templates
            customizable at skill level : in the associated CMP document
          DOC are:
            defined at project level: /srv/trac/<proj>/templates
          ECM are:
            defined at project level: /srv/trac/<proj>/templates
          FEE are:
            defined at project level: /srv/trac/<proj>/templates
    """

    cat_type = 'tags_mgmt'
    _cat_label = 'Conf Mgmt'

    page_type = 'templates'
    page_label = (N_('Template'), N_('Templates'))

    # IAdminPanelProvider methods

    def get_admin_panels(self, req):
        if 'TICKET_VIEW' in req.perm:
            yield (self.cat_type,
                   self._cat_label,
                   self.page_type,
                   self.page_label[1])

    def render_admin_panel(self, req, cat, page, path_info):
        my_script_list = glob.glob('%s/../htdocs/stamped/admin_*' % os.path.dirname(os.path.realpath(__file__)))
        if len(my_script_list) != 1:
            raise TracError(_("More than one admin.js script or none."))
        else:
            add_script(req, 'artusplugin/stamped/%s' % os.path.basename(my_script_list[0]))
        req.perm.require('TICKET_VIEW')

        if req.method == 'POST':
            if 'cancel' not in req.args and 'selected' in req.args:
                selected = req.args.get('selected')
                self.config.set('artusplugin',
                                '%s_form_template_dir' % selected,
                                req.args.get('%s_selected_url' % selected))
                try:
                    self.config.save()
                    add_notice(req, _('Your change has been saved.'))
                except Exception:
                    e = sys.exc_info()[1]
                    try:
                        self.log.error('Error writing to trac.ini: %s',
                                    exception_to_unicode(e))
                        add_warning(req, _('Error writing to trac.ini, '
                                        'make sure it is writable by '
                                        'the web server. Your change '
                                        'has not been saved.'))
                    finally:
                        del e

            req.redirect(req.href.admin(cat, page))

        data = {}
        data['skills'] = self.env.config.get(
            'ticket-custom', 'skill.options').split('|')
        data['ticket_types'] = [ttype.name for ttype in Type.select(self.env)]
        ticket_suffixes = util.get_prop_values(self.env, 'ticket_suffix')
        data['ticket_types_with_forms'] = sorted([ttype.name for ttype in Type.select(self.env)
                                           if ttype.name in ticket_suffixes])

        selected_skill = None
        selected_ttype = None
        selected_url = None

        if 'selected_url' in req.args and 'caller' in req.args and isinstance(req.args.get('caller'), str):
            caller = req.args.get('caller')
            regexp = r"\A(%s)_(%s)\Z" % ('|'.join(data['skills']),
                                         '|'.join(data['ticket_types_with_forms']))
            match = re.search(regexp, caller)
            if match:
                selected_skill = match.group(1)
                selected_ttype = match.group(2)
                selected_url = req.args.get('selected_url')

        program_data = util.get_program_data(self.env)
        trac_env_name = program_data['trac_env_name']
        program_path = '/var/cache/trac/tickets/%s' % trac_env_name
        data['configurable'] = {}
        data['template_filenames'] = {}
        data['selected'] = {}
        data['source_path'] = {}
        data['urltracbrowse'] = {}
        data['disabled'] = {}
        data['link_url'] = {}
        data['prefix'] = {}
        for ttype in data['ticket_types_with_forms']:
            template_cls = TicketFormTemplate.get_subclass(self.env,
                                                           ttype)
            data['configurable'][ttype] = template_cls.configurable
            if data['configurable'][ttype]:
                data['template_filenames'][ttype] = {}
                data['selected'][ttype] = {}
                data['source_path'][ttype] = {}
                data['urltracbrowse'][ttype] = {}
                data['disabled'][ttype] = {}
                data['link_url'][ttype] = {}
                data['prefix'][ttype] = {}
                for skill in data['skills']:
                    template = template_cls(self.env,
                                            ttype,
                                            program_path,
                                            skill)
                    data['template_filenames'][ttype][skill] = template.name
                    data['selected'][ttype][skill] = False
                    if template.source_rev:
                        data['source_path'][ttype][skill] = '%s?rev=%s' % (
                            template.source_path, template.source_rev)
                    else:
                        data['source_path'][ttype][skill] = template.source_path
                    data['prefix'][ttype][skill] = template.source_path.split('/')[1]
                    if data['prefix'][ttype][skill] == 'srv':
                        repo_path = '/'
                    else:
                        repo_path = data['source_path'][ttype][skill]
                    data['urltracbrowse'][ttype][skill] = util.get_tracbrowserurl(
                        self.env, repo_path, caller='%s_%s' % (skill, ttype))
                    data['disabled'][ttype][skill] = None
                    if data['prefix'][ttype][skill] == 'srv':
                        data['link_url'][ttype][skill] = '/templates/%s/' % trac_env_name
                    else:
                        data['link_url'][ttype][skill] = util.get_tracbrowserurl(
                            self.env, '%s?rev=%s' % (template.source_path, template.source_rev))
                    if selected_url:
                        if ttype == selected_ttype and skill == selected_skill:
                            data['selected'][ttype][skill] = True
                            data['source_path'][ttype][skill] = selected_url
                            data['urltracbrowse'][ttype][skill] = util.get_tracbrowserurl(
                                self.env, selected_url, caller='%s_%s' % (skill, ttype))
                        else:
                            data['disabled'][ttype][skill] = 'disabled'
            else:
                default_skill = self.env.config.get('artusplugin', 'default_skill', 'SYS')
                template = template_cls(self.env,
                                        ttype,
                                        program_path,
                                        default_skill)
                data['template_filenames'][ttype] = template.name
                data['source_path'][ttype] = template.source_path
                data['urltracbrowse'][ttype] = util.get_tracbrowserurl(
                    self.env, data['source_path'][ttype])

        # ECM / FEE / DOC tickets
        for ttype in ('ECM', 'FEE', 'DOC'):
            if ttype in [t.name for t in Type.select(self.env)]:
                data['template_filenames'][ttype] = self.env.config.get(
                    'artusplugin', '%s_template' % ttype)
                for template_dir in Chrome(self.env).get_templates_dirs():
                    source_file = '%s/%s' % (template_dir, data['template_filenames'][ttype])
                    if os.path.exists(source_file):
                        source_path = template_dir
                        break
                else:
                    source_path = None
                data['source_path'][ttype] = source_path
                if source_path:
                    data['prefix'][ttype] = source_path.split('/')[1]
                    if data['prefix'][ttype] == 'srv':
                        data['link_url'][ttype] = '/templates/%s/' % trac_env_name
                    else:
                        data['link_url'][ttype] = None

        return 'ticketformtemplates.html', data


class ReferencesMgmt(CComponent):
    """ Admin panels for References Management. """

    implements(IAdminPanelProvider)

    cat_type = 'tags_mgmt'
    _cat_label = 'Conf Mgmt'
    page_type = 'references'
    page_label = ('Reference', 'References')

    # IAdminPanelProvider

    def get_admin_panels(self, req):
        if 'VERSION_TAG_DELETE' in req.perm:
            yield (self.cat_type, self._cat_label, self.page_type, self.page_label[1])
        
    # TicketAdminPanel methods

    def render_admin_panel(self, req, cat, page, reference):
        my_script_list = glob.glob('%s/../htdocs/stamped/admin_*' % os.path.dirname(os.path.realpath(__file__)))
        if len(my_script_list) != 1:
            raise TracError(_("More than one admin.js script or none."))
        else:
            add_script(req, 'artusplugin/stamped/%s' % os.path.basename(my_script_list[0]))
        Chrome(self.env).add_wiki_toolbars(req)

        # Detail view?
        if reference:
            ref = model.Reference(self.env, reference)
            if req.method == 'POST':
                if req.args.get('save'):
                    ref.artusref = artusref = req.args.get('artusref')
                    ref.customerref = req.args.get('customerref')
                    try:
                        ref.update()
                    except self.env.db_exc.IntegrityError:
                        raise TracError(_('Reference "%(artusref)s" already '
                                          'exists.', artusref=artusref))
                    add_notice(req, _('Your changes have been saved.'))
                    req.redirect(req.href.admin(cat, page))
                elif req.args.get('cancel'):
                    req.redirect(req.href.admin(cat, page))

            data = {'view': 'detail', 'reference': ref}

        else:
            if req.method == 'POST':
                # Add reference
                if req.args.get('add') and req.args.get('artusref'):
                    artusref = req.args.get('artusref')
                    try:
                        ref = model.Reference(self.env, artusref=artusref)
                    except ResourceNotFound:
                        ref = model.Reference(self.env)
                        ref.artusref = artusref
                        if req.args.get('customerref'):
                            ref.customerref = req.args.get('customerref')
                        # First reference is default reference
                        if len(list(model.Reference.select(self.env))) == 0:
                            ref.default = 1
                        ref.insert()
                        add_notice(req, _('The reference "%(artusref)s" has been '
                                          'added.', artusref=artusref))
                        req.redirect(req.href.admin(cat, page))
                    else:
                        if ref.artusref is None:
                            raise TracError(_("Invalid artus reference."))
                        raise TracError(_('Reference "%(artusref)s" already '
                                          'exists.', artusref=artusref))

                # Remove references
                elif req.args.get('remove'):
                    sel = req.args.get('sel')
                    if not sel:
                        raise TracError(_('No reference selected'))
                    if not isinstance(sel, list):
                        sel = [sel]
                    with self.env.db_transaction:
                        for ref in model.Reference.select(self.env, where_expr_list=["artusref IN ('%s')" % "','".join(sel)],
                                                          ordering_term='[default] ASC'):
                            ref.delete()
                            # Try to replace last removed reference in case it was the default one 
                            if ref.default == 1:
                                remaining_refs = list(model.Reference.select(self.env))
                                if len(remaining_refs) == 1:
                                    remaining_refs[0].default = 1
                                    remaining_refs[0].update()

                    add_notice(req, _("The selected references have been "
                                      "removed."))
                    req.redirect(req.href.admin(cat, page))

                # Set default reference
                elif req.args.get('apply'):
                    artusref = req.args.get('default')
                    if artusref:
                        self.log.info("Setting default reference to %s", artusref)
                        # Uncheck
                        for ref in model.Reference.select(self.env, where_expr_list=['[default]=1']):
                            ref.default = 0
                            ref.update() 
                        # Check
                        ref = model.Reference(self.env, artusref=artusref)
                        ref.default = 1
                        ref.update()
                        req.redirect(req.href.admin(cat, page))

            data = {'view': 'list',
                    'references': list(model.Reference.select(self.env))}

        return 'references.html', data


class UsersAdminPanel(CComponent):
    """
      Displays users that can access this project.
      Users are listed first by profile - with ascending rights,
      then by role - when applicable,
      and finally alphabetically.
    """

    implements(IAdminPanelProvider)

    def __init__(self):
        CComponent.__init__(self)

    # IAdminPanelProvider methods

    def get_admin_panels(self, req):
        yield ('general', 'General', 'users', 'Users')

    def render_admin_panel(self, req, cat, page, path_info):

        users = util.Users(self.env)
        return 'users.html', {'user_profiles': users.user_profiles,
                              'user_roles': users.user_roles,
                              'displayed_roles': users.displayed_roles,
                              'role_initials': users.role_initials,
                              'roles_by_profile': users.roles_by_profile,
                              'users_by_profile': users.users_by_profile,
                              'users_by_role': users.users_by_role,
                              'users_with_role_by_profile': users.users_with_role_by_profile,
                              'users_without_role_by_profile': users.users_without_role_by_profile,
                              'registered_users': users.registered_users,
                              'test_users': users.test_users,
                              'project_users': users.project_users,
                              'ldap_display_names': users.users_ldap_names,
                              'users_emails': users.users_emails}


