# -*- coding: utf-8 -*-
#
# Copyright (C) 2016 Artus
# All rights reserved.
#
# Author: Michel Guillot <michel.guillot@meggitt.com>

""" OpenOffice form handling """

from __builtin__ import unicode

# ODFPY
from odf.form import Form, Checkbox
from odf.opendocument import load
from odf.table import Table

# pylocker
import uuid
from pylocker import Locker

# Genshi
from genshi.builder import tag
from genshi.util import striptags

# Trac
from trac.attachment import Attachment
from trac.core import TracError
from trac.ticket import Ticket
from trac.ticket.web_ui import TicketModule
from trac.util.datefmt import localtz, utc
from trac.util.text import unicode_quote
from trac.versioncontrol.api import NoSuchNode, RepositoryManager
from trac.web.chrome import Chrome

# Standard lib
from backports.tempfile import TemporaryDirectory
from datetime import datetime
from xml.dom.minidom import parseString, parse
from lxml import etree as ElementTree
import codecs
import filecmp
import fnmatch
import hashlib
import os
import re
import shutil
import sys
import syslog
import tempfile
import urllib2
import zipfile

# Same package
from artusplugin import util, Ooo, _
from artusplugin.model import NamingRule


class TicketFormTemplate(object):
    """ Ticket form template handling """

    @staticmethod
    def get_subclass(env, ticket_type):
        """ Return subclass associated to ticket type """
        office_suite = util.get_prop_values(env,
                                            'ticket_edit.office_suite')[ticket_type]
        return formtemplate_subclasses[office_suite]

    def __init__(self, env, ticket_type, program_path, skill):
        self.env = env
        self.ticket_type = ticket_type
        program_data = util.get_program_data(env)
        self.trac_env_name = program_data['trac_env_name']
        self.program_name = program_data['program_name']
        self.program_path = program_path
        self.skill = skill
        self.suffix = util.get_prop_values(env, 'ticket_suffix')[ticket_type]
        self.env_templates_dir = Chrome(env).get_templates_dirs()[0]
        self.shared_templates_dir = Chrome(env).get_templates_dirs()[1]

    def get_link(self):
        hostname = util.get_hostname(self.env)
        scheme = self.env.config.get('artusplugin', 'scheme')
        host_url = '%s://%s' % (scheme, hostname)
        if '/usr' in self.source_path:
            level = 'TRAC'
            location = '[%s/templates/share/ %s]' % (host_url, self.source_path)
        elif '/srv' in self.source_path:
            level = 'project'
            location = '[%s/templates/%s/ %s]' % (host_url, self.trac_env_name, self.source_path)
        else:
            if self.__class__.configurable:
                level = 'skill'
            else:
                level = 'project'
            repo_dir = util.get_url(self.source_path).rsplit('/', 1)[-1]
            location = '[/browser%s?rev=%s %s @ %s]' % (
                self.source_path.replace(' ', '%20'),
                self.source_rev,
                repo_dir.replace(' ', '%20'),
                self.source_rev)

        templatelink = '%s template (%s): %s' % (
            self.ticket_type,
            'defined at %s level' % level,
            location)

        return templatelink


class OOoFormTemplate(TicketFormTemplate):
    """ OpenOffice.org ticket form template handling """

    # Location may not be changed by the user (no Browse button)
    configurable = False

    def __init__(self, env, ticket_type, program_path, skill):
        super(OOoFormTemplate, self).__init__(env,
                                              ticket_type,
                                              program_path,
                                              skill)
        templates_dir = env.config.get('artusplugin', 'forms_template_dir')
        self.name = '%s_%s_TEMPLATE.%s' % (self.ticket_type,
                                           self.program_name,
                                           self.suffix)
        self.source_path = '%s/%s_%s_TEMPLATE' % (templates_dir,
                                                  self.ticket_type,
                                                  self.program_name)
        repos = util.get_repository(env, self.source_path)
        self.source_rev = util.get_head_revision(repos)
        self.source_name = '%s/%s' % (self.source_path, self.name)
        self.repo_form = True
        self.http_url = util.get_repo_url(self.env, self.source_path)
        self.http_fileurl = util.get_repo_url(self.env, self.source_name)
        self.cache_root = '%s/01-Templates' % program_path
        self.cache_path = '%s/%s_%s_TEMPLATE' % (self.cache_root,
                                                 self.ticket_type,
                                                 self.program_name)
        self.cache_name = '%s/%s' % (self.cache_path, self.name)

    def setup(self, doc_tag):
        """ No setup is necessary """
        pass


class MSOFormTemplate(TicketFormTemplate):
    """ MS Office ticket form template handling """

    # Location may be changed by the user (Browse button)
    configurable = True

    @staticmethod
    def get_template_filenames(env, ticket_type, ticket_skill, ticket_suffix):
        default_template_filename = '%s.%s' % (ticket_type, ticket_suffix)
        standard_template_confname = '%s_template' % ticket_type
        standard_template_filename = env.config.get('artusplugin',
                                                    standard_template_confname,
                                                    default_template_filename)
        custom_template_confname = ('%s_template' % ticket_type
                                    if ticket_type == 'EFR'
                                    else
                                    '%s_%s_template' % (ticket_type, ticket_skill))
        effective_template_filename = env.config.get('artusplugin',
                                                     custom_template_confname,
                                                     standard_template_filename)
        return standard_template_filename, effective_template_filename

    def __init__(self, env, ticket_type, program_path, skill):
        super(MSOFormTemplate, self).__init__(env,
                                              ticket_type,
                                              program_path,
                                              skill)
        self.schemas_url = env.config.get("artusplugin", "schemas_url")
        self.schemas_url_legacy_list = [url.strip() for url in env.config.get("artusplugin", "schemas_url_legacy_list").split(',')]
        self.ns0 = "%s/%s" % (self.schemas_url, ticket_type)
        self.ns0_legacy_list = ["%s/%s" % (url, ticket_type) for url in self.schemas_url_legacy_list]
        self.root_tag = "{%s}%s" % (self.ns0, ticket_type)
        self.root_tag_legacy_list = ["{%s}%s" % (ns0, ticket_type) for ns0 in self.ns0_legacy_list]
        if ticket_type == "MOM":
            # MOM template is a LibreOffice template
            # It is filled as such then converted to docx
            self.suffix = "odt"
        # Effective template filename
        self.name = self.get_template_filenames(self.env, ticket_type, skill, self.suffix)[1]
        ini_source_path = self.env.config.get('artusplugin',
                                              ('%s_%s_form_template_dir' % (skill, ticket_type) if skill
                                               else '%s_form_template_dir' % ticket_type),
                                              self.shared_templates_dir)
        self.source_path = util.get_url(ini_source_path)
        self.source_rev = util.get_revision(ini_source_path)
        self.source_name = '%s/%s' % (self.source_path, self.name)
        self.repo_form = self.source_path.split('/')[1] not in ('srv', 'usr')
        if self.repo_form:
            self.http_url = util.get_repo_url(self.env,
                                              '%s@%s' % (self.source_path,
                                                         self.source_rev))
            self.http_fileurl = util.get_repo_url(
                self.env,
                '%s@%s' % (self.source_name,
                           self.source_rev))
        else:
            self.http_url = None
            self.http_fileurl = None
        self.cache_root = '%s/templates' % program_path
        self.cache_path = self._get_cache_path()
        self.cache_name = '%s/%s' % (self.cache_path, self.name)
        if ticket_type in ('EFR', 'ECR'):
            self.vbaprojectfile = 'word/vbaProject.bin'
            self.vbasignaturefile = 'word/vbaProjectSignature.bin'
            self.vbadatafile = 'word/vbaData.xml'
        elif ticket_type == 'PRF':
            self.vbaprojectfile = 'xl/vbaProject.bin'
            self.vbasignaturefile = 'xl/vbaProjectSignature.bin'

    def _get_cache_path(self):
        """ Returns ticket template cache directory """
        if self.repo_form:
            source_dir = self.source_path.rsplit('/', 1)[-1]
        else:
            source_dir = self.skill
        return '%s/%s' % (self.cache_root, source_dir)

    def get_cache_name(self, tmpdir, template_path):
        cache_name = '%s/%s' % (tmpdir, self.name)
        if self.repo_form:
            repos = util.get_repository(self.env, template_path)
            node = repos.get_node(template_path, self.source_rev)
            templatefile = os.fdopen(os.open(cache_name,
                                             os.O_CREAT + os.O_WRONLY + os.O_TRUNC,
                                             0666), 'w')
            shutil.copyfileobj(node.get_content(), templatefile)
            templatefile.close()
        else:
            shutil.copy(template_path, cache_name)
        return cache_name

    def get_customxml_filepath(self, dir):
        item_dirpath = '%s/customXml' % dir
        for item_filename in fnmatch.filter(os.listdir(item_dirpath), 'item[0-9].xml'):
            item_filepath = "%s/%s" % (item_dirpath, item_filename)
            customxml_root_elt = ElementTree.parse(item_filepath).getroot()
            if customxml_root_elt.tag in [self.root_tag] + self.root_tag_legacy_list:
                return item_filepath
        else:
            message = tag.span('%s: The custom XML could not be identified in the following directory:', util.lineno())
            message(tag.p(item_dirpath))
            raise TracError(message, 'Hostname mismatch ?' % True)

    def _unzip(self, filename, tmpdir):
        """ Open docm/xlsm """
        with zipfile.ZipFile(filename, "r") as zip_ref:
            zip_ref.extractall(tmpdir)

    def _zip(self, filename, tmpdir):
        """ Close docm/xlsm """
        util.zip_dir(tmpdir, filename)

    def get_creation_datetime(self, template_path):
        with TemporaryDirectory() as tmpdir:
            self._unzip(self.get_cache_name(tmpdir, template_path), tmpdir)
            customxml_dom = parse(self.get_customxml_filepath(tmpdir))
            lst = customxml_dom.getElementsByTagName("Template")
            if lst:
                elt = lst[0]
                return elt.getAttribute("CreationDateTime")
            else:
                return None

    def get_modification_datetime(self, template_path):
        with TemporaryDirectory() as tmpdir:
            self._unzip(self.get_cache_name(tmpdir, template_path), tmpdir)
            customxml_dom = parse(self.get_customxml_filepath(tmpdir))
            lst = customxml_dom.getElementsByTagName("Template")
            if lst:
                elt = lst[0]
                return elt.getAttribute("ModificationDateTime")
            else:
                return None

    def get_vba_hash(self, template_path):
        hash_md5 = hashlib.md5()
        with TemporaryDirectory() as tmpdir:
            self._unzip(self.get_cache_name(tmpdir, template_path), tmpdir)
            vba_filename = '%s/%s' % (tmpdir, self.vbaprojectfile)
            with open(vba_filename, "rb") as f:
                for chunk in iter(lambda: f.read(4096), b""):
                    hash_md5.update(chunk)
        return hash_md5.hexdigest()


formtemplate_subclasses = {'OpenOffice': OOoFormTemplate,
                           'MS Office': MSOFormTemplate}


class TicketForm(object):
    """ This class and its subclasses are used for handling the ticket form:
        editing, archiving

    Objects of this class should not be instantiated directly.
    """

    @staticmethod
    def get_subclass(env, ticket_type, ticket_subtype):
        """ Return subclass associated to ticket type and ticket sub type"""
        office_suite = util.get_prop_values(
            env,
            'ticket_edit.office_suite')[ticket_type]
        if ticket_subtype:
            return ticketform_subclasses[office_suite][ticket_type][ticket_subtype]
        else:
            return ticketform_subclasses[office_suite][ticket_type]

    @staticmethod
    def get_ticket_process_data(env, authname, ticket):
        """ Get ticket data necessary when ticket is created or updated."""

        data = {}
        data['authname'] = authname
        data['id'] = str(ticket.id)
        data['ticket_type'] = ticket['type']
        data['base_path'] = env.base_url

        program_data = util.get_program_data(env)
        data['trac_env_name'] = program_data['trac_env_name']
        data['program_name'] = program_data['program_name']

        # Get Ticket Identifier (summary)
        data['ticket_id'] = ticket['summary']

        # Get ticket form data
        tf_subclass = TicketForm.get_subclass(
            env,
            data['ticket_type'],
            (data['ticket_type'] == 'MOM' and 'momtype' in ticket.values and
             ticket['momtype']) or None)
        data['ticket_form'] = tf_subclass(
            env,
            data['ticket_type'],
            data['id'],
            data['ticket_id'],
            ticket['skill'],
            'document' in ticket.values and ticket['document'] or None,
            authname)

        # Get ticket template data
        tft_subclass = TicketFormTemplate.get_subclass(
            env,
            data['ticket_type'])
        data['ticket_form_template'] = tft_subclass(
            env,
            data['ticket_type'],
            data['ticket_form'].program_path,
            ticket['skill'])

        # Get the attachments data
        data['attachment'] = TicketAttachment(
            env,
            data['ticket_form'],
            data['id'])

        # Get SVN template command
        data['svn_template_cmd'] = util.SVN_TEMPLATE_CMD

        # Get SVNMUCC template command
        data['svnmucc_template_cmd'] = util.SVNMUCC_TEMPLATE_CMD

        return data

    @staticmethod
    def get_closure_decision_ground(env, ticket, comment):
        if ticket['resolution'] == 'fixed':
            if ticket['type'] in ('ECR', 'RF', 'PRF'):
                changeset_id = ''
                changesets = re.findall(r'changeset:(\d+)', comment)
                if changesets:
                    changesets.sort(key=lambda x: int(x))
                    try:
                        rm = RepositoryManager(env)
                        rn = util.get_repo_name(env, ticket['skill'])
                        repos = rm.get_repository(rn)
                        repos.get_changeset(changesets[-1])
                        changeset_id = changesets[-1]
                    except Exception:
                        pass
                return changeset_id
            elif ticket['type'] in ('EFR'):
                if ticket['status'] == 'closed':
                    ticket_module = TicketModule(env)
                    changes = [change for change in
                               ticket_module.grouped_changelog_entries(ticket)]
                    # Search for tech comment
                    tech_comment = ''
                    for change in reversed(changes):
                        if 'resolution' in change['fields']:
                            continue
                        if 'status' in change['fields']:
                            if (change['fields']['status']['old'] ==
                                '07-assigned_for_closure_actions' and
                                change['fields']['status']['new'] ==
                                'closed'):
                                continue
                            else:
                                break
                        if 'owner' in change['fields']:
                            tech_comment = change['comment']
                            break
                    if tech_comment:
                        comment = '%s\n%s' % (tech_comment, comment)
                    return comment
                else:
                    return comment
            else:
                return comment
        elif ticket['resolution'] == 'change requested':
            match = re.findall(r'ticket:([1-9]\d*)', comment)
            ecr_list = None
            for tkt in match:
                try:
                    tckt = Ticket(env, tkt)
                    if tckt['type'] == 'ECR':
                        if ecr_list:
                            ecr_list += ",\n" + tckt['summary']
                        else:
                            ecr_list = tckt['summary']
                    else:
                        continue
                except Exception:
                    continue
            if not ecr_list:
                ecr_list = ""
            return ecr_list
        elif ticket['resolution'] == 'rejected':
            return comment
        else:
            return comment

    @staticmethod
    def tickets_pdf_convert(env, trac_env_name, authname, ticket_list):
        base_dir = '/var/cache/trac/PDF-printing/%s' % trac_env_name
        # Case where only one element is selected
        if not type(ticket_list) == list:
            ticket_list = [ticket_list]
        # Creates base_dir and intermediate directories if they don't exist
        if not os.access(base_dir, os.F_OK):
            os.makedirs(base_dir)
        # The job will be executed under a temporary sub-directory of base_dir
        # named as follows: <trac_env_name>_<datetime>_<authname>
        base_path = '%s/%s_%s_%s' % (base_dir,
                                     trac_env_name,
                                     unicode(datetime.now(localtz).strftime(
                                         '%Y-%m-%d_%H-%M-%S')),
                                     authname)
        if not os.access(base_path, os.F_OK):
            os.mkdir(base_path)

        # The list of tickets is extracted from the repository,
        # forms are converted to PDF and an archive is made
        # Duplicates are eliminated
        seen = set()
        ticket_list = [tid
                       for tid in ticket_list
                       if tid not in seen and not seen.add(tid)]

        for trac_id in ticket_list:
            ticket = Ticket(env, trac_id)
            tp_data = TicketForm.get_ticket_process_data(env, authname, ticket)
            tf = tp_data['ticket_form']
            pdf_path = tf.pdf_convert(ticket, base_path)

            # An archive is made incrementally
            if pdf_path:
                cmd = """\
/usr/bin/zip -j "%s/%s.zip" "%s" -c <<EOF
Ticket TRAC #%s
EOF
                """ % (base_path,
                       os.path.basename(base_path),
                       pdf_path,
                       trac_id)
                unix_cmd_list = [cmd]
                # Effective application of the list of commands
                util.unix_cmd_apply(env, unix_cmd_list, util.lineno())

        # The finalized archive becomes available
        original_path = '%s/%s.zip' % (base_path, os.path.basename(base_path))
        final_path = '%s/%s.zip' % (base_dir, os.path.basename(base_path))
        if os.access(original_path, os.F_OK):
            os.rename(original_path, final_path)
            # Job completed: send an email to the client
            util.send_pdf_print_email(env, authname, base_path)

    def __init__(self, env, ticket_type, tid, ticket_id, skill, tagname, authname):
        self.env = env
        self.ticket_type = ticket_type
        program_data = util.get_program_data(env)
        self.trac_env_name = program_data['trac_env_name']
        self.program_name = program_data['program_name']
        self.id = tid
        self.ticket_id = ticket_id
        self.skill = skill
        self.repo = util.get_repo_name(env, skill)
        self.tagname = tagname
        self.authname = authname
        self.office_suite = util.get_prop_values(env, 'ticket_edit.office_suite')[ticket_type]
        self.suffix = util.get_prop_values(env, 'ticket_suffix')[ticket_type]
        self.edit_suffix = util.get_prop_values(env, 'ticket_edit.suffix')[ticket_type]
        self.mine_suffix = util.get_prop_values(env, 'ticket_mine.suffix')[ticket_type]
        self.program_path = '/var/cache/trac/tickets/%s' % self.trac_env_name
        self.user_path = '%s/%s' % (self.program_path, authname)
        self.type_path = '%s/%s' % (self.user_path, self.ticket_type)
        self.type_subpath = self._get_type_subpath()
        self.path = '%s/t%s' % (self.type_subpath, self.id)
        self.lock_path = '%s/.lock' % self.path
        self.oldcontent_path = self.path
        self.oldcontent_filename = '%s/%s.%s' % (self.oldcontent_path,
                                                 self.ticket_id, self.suffix)
        self.copycontent_path = '%s/.trac' % self.path
        self.copycontent_filename = '%s/%s.%s' % (self.copycontent_path, self.ticket_id, self.edit_suffix)
        self.minecontent_filename = '%s/%s.%s' % (
            self.path, self.ticket_id, self.mine_suffix)
        self.label = self._get_label(ticket_type)
        self.http_protocol = self.env.config.get('artusplugin', 'scheme')
        self.webdav_protocol = util.get_prop_values(env, 'ticket_edit.protocol')[self.ticket_type]
        if self.http_protocol == 'https':
                self.webdav_protocol += 's'

    def _get_type_subpath(self):
        if self.ticket_type in ('ECR', 'MOM'):
            return '%s/%s' % (self.type_path, self.skill)
        elif self.ticket_type in ('RF', 'PRF'):
            return '%s/%s' % (self.type_path, self.tagname)
        else:
            return self.type_path

    def _get_label(self, ticket_type):
        if ticket_type == 'EFR':
            return 'Problem data'
        elif ticket_type == 'ECR':
            return 'Change data'
        elif ticket_type in ('RF', 'PRF'):
            return 'Verification data'
        elif ticket_type == 'MOM':
            return 'Meeting data'

    def _get_webdav_url(self):
        webdav_url = '%s?action=edit&url=%s://%s/tickets/%s/%s/%s' % (
            self.clickonce_app_url,
            self.webdav_protocol,
            util.get_hostname(self.env),
            self.trac_env_name,
            self.authname,
            self.ticket_type)
        if self.ticket_type in ('ECR', 'MOM'):
            webdav_url = '%s/%s' % (webdav_url, self.skill)
        elif self.ticket_type in ('RF', 'PRF'):
            webdav_url = '%s/%s' % (webdav_url, self.tagname)
        webdav_url = unicode_quote(webdav_url, ':/?=&@')
        return webdav_url

    def _get_wc_url(self):
        wc_url = '%s://%s/tickets/%s/%s/%s' % (
            self.http_protocol,
            util.get_hostname(self.env),
            self.trac_env_name,
            self.authname,
            self.ticket_type)
        if self.ticket_type in ('ECR', 'MOM'):
            wc_url = '%s/%s' % (wc_url, self.skill)
        elif self.ticket_type in ('RF', 'PRF'):
            wc_url = '%s/%s' % (wc_url, self.tagname)
        wc_url = unicode_quote(wc_url, ':/?=&@')
        return wc_url

    def _set_paths(self):
        self.ticket_path = '%s/%s' % (self.repo_subpath, self.ticket_id)
        self.ticket_filename = '%s/%s.%s' % (self.ticket_path, self.ticket_id, self.suffix)

    def _set_urls(self):
        self.repo_url = util.get_repo_url(self.env, self.repo_path)
        self.repo_suburl = util.get_repo_url(self.env, self.repo_subpath)
        self.http_url = '%s/%s' % (self.repo_suburl, self.ticket_id)
        self.http_fileurl = '%s/%s.%s' % (self.http_url, self.ticket_id, self.suffix)

    def get_update_deployment_date(self):
        return datetime(2008, 7, 21, 9, 0, 0, tzinfo=utc)

    def upgrade(self, tft):
        """ Not implemented by default """

    def modified(self):
        if os.access(self.path, os.F_OK):
            #  create a unique lock pass. This can be any string.
            lpass = str(uuid.uuid1())

            # create locker instance
            FL = Locker(filePath=None, lockPass=lpass, lockPath=self.lock_path)

            # acquire the lock - protect against several concurrent processes
            with FL as r:
                # r is a tuple of three items. the acquired result, the acquiring code and
                # a file descriptor fd. fd will always be None when filePath is None.
                acquired, code, fd = r

                # check if acquired.
                if acquired:
                    # safely check the edit form
                    if (os.access(self.content_filename, os.F_OK) and
                        os.access(self.copycontent_filename, os.F_OK)):
                        if filecmp.cmp(self.content_filename, self.copycontent_filename):
                            return False
                        else:
                            return True
                    else:
                        return False
                else:
                    raise TracError(_("Unable to acquire the lock. Exit code %s" % code))

            # no need to release anything because with statement takes care of that.
        else:
            # nothing to protect
            return False

    def prepare_edit_form(self, setup_data=None):
        if os.access(self.path, os.F_OK):
            #  create a unique lock pass. This can be any string.
            lpass = str(uuid.uuid1())

            # create locker instance
            FL = Locker(filePath=None, lockPass=lpass, lockPath=self.lock_path)

            # acquire the lock - protect against several concurrent processes
            with FL as r:
                # r is a tuple of three items. the acquired result, the acquiring code and
                # a file descriptor fd. fd will always be None when filePath is None.
                acquired, code, fd = r

                # check if acquired.
                if acquired:
                    # safely prepare the edit form
                    if not os.access(self.content_path, os.F_OK):
                        os.mkdir(self.content_path)
                    if os.access(self.content_filename, os.F_OK):
                        os.remove(self.content_filename)
                    shutil.copy(self.oldcontent_filename, self.content_filename)
                    if setup_data:
                        self.update(*setup_data)
                    if not os.access(self.copycontent_path, os.F_OK):
                        os.mkdir(self.copycontent_path)
                    elif self.office_suite == 'MS Office':
                        if os.access(self.copyform_filename, os.F_OK):
                            os.remove(self.copyform_filename)
                    if os.access(self.copycontent_filename, os.F_OK):
                        os.remove(self.copycontent_filename)
                    shutil.copy(self.content_filename, self.copycontent_filename)
                else:
                    raise TracError(_("Unable to acquire the lock. Exit code %s" % code))

            # no need to release anything because with statement takes care of that.

    def pdf_convert(self, ticket, base_path):
        repos = util.get_repository(self.env, self.repo_path)
        if repos is None:
            return None
        try:
            # Extract ticket form and associated attachments
            node = repos.get_node('%s/%s' % (self.repo_subpath, ticket['summary']))
            util.create_fs_from_repo(base_path, node)
            ticket_path = '%s/%s' % (base_path, node.name)
            ticket_filename = "%s/%s.%s" % (ticket_path,
                                            node.name,
                                            self.suffix)
            # Revision added to ticket Id
            revision = util.get_revision_from_description(
                ticket['summary'],
                ticket['description'])
            updated_id = '%s @ %s' % (ticket['summary'], revision)
            # Update ticket form
            self.prepare_pdf(ticket_filename, updated_id)
            # Convert ticket form to PDF
            unix_cmd_list = ['%s/program/python /srv/trac/common/DocumentConverter.py '
                             '"%s/%s.%s" "%s/%s.pdf" %s' %
                             (self.pdf_converter_install_dir,
                              ticket_path,
                              node.name,
                              self.suffix,
                              ticket_path,
                              node.name,
                              self.pdf_converter_port)]
            # Effective application of the list of commands -
            # NOTE: Conversion of ticket and attachment are done separately
            # because of sporadic segmentation faults (-11) on tickets
            # which would prevent attachments conversion if done together
            retcode = util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())[0]
            if retcode == 0:
                attachments_path = '%s/attachments' % ticket_path
                if os.path.exists(attachments_path):
                    attachments_list = os.listdir(attachments_path)
                    if attachments_list:
                        attachments_to_be_deleted = []
                        attachments_to_be_converted = []
                        # Do not include symbolic links
                        # Do not include attachments which are OOo sources
                        # or MS sources because they are converted to PDF

                        suffx = util.get_prop_values(
                            self.env,
                            'attachment_edit.office_suite')
                        for fn in attachments_list:
                            if isinstance(fn, str):
                                fn = fn.decode('utf-8')
                            path = "%s/%s" % (attachments_path, fn)
                            if os.path.islink(path.encode('utf-8')):
                                attachments_to_be_deleted.append(fn)
                            elif fn.split('.')[-1] in suffx.keys():
                                attachments_to_be_converted.append(fn)
                                attachments_to_be_deleted.append(fn)
                        # Convert supported attachments to PDF
                        unix_cmd_list = []
                        for fn in attachments_to_be_converted:
                            unix_cmd_list += ['%s/program/python /srv/trac/common/DocumentConverter.py '
                                              '"%s/%s" "%s/%s.pdf" %s' %
                                              (self.env.config.get('artusplugin', 'LOo_install_dir'),
                                               attachments_path,
                                               fn,
                                               attachments_path,
                                               urllib2.unquote(fn.rsplit('.', 1)[-2]),
                                               self.env.config.get('artusplugin', 'LOo_port'))]
                        # Effective application of the list of commands
                        util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())
                        # Removes attachments not included into the PDF file
                        for fn in attachments_to_be_deleted:
                            os.remove("%s/%s" % (attachments_path, fn))
                        # Include remaining attachment(s) into the PDF
                        util.pdf_attach_files("%s/%s.pdf" % (ticket_path, node.name),
                                              ["%s/%s" % (attachments_path, attachment)
                                               for attachment in os.listdir(attachments_path)],
                                              "%s/_%s.pdf" % (ticket_path, node.name))
                        if os.access('%s/_%s.pdf' % (ticket_path, node.name), os.F_OK):
                            os.rename('%s/_%s.pdf' % (ticket_path, node.name),
                                      '%s/%s.pdf' % (ticket_path, node.name))
                return '%s/%s.pdf' % (ticket_path, node.name)

        except NoSuchNode:
            return None  # ignore broken repositories used for testing
        except Exception:
            exc_info = sys.exc_info()
            exc_obj = exc_info[1]
            exc_tb = exc_info[2]
            fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
            syslog.syslog("Unexpected error: %s:%s %s" % (fname, exc_tb.tb_lineno, exc_obj.message.encode('utf-8')))
            raise

    def status(self, status):
        """
            Gets back data on working copy status (status = 'wc-status')
            or repository status (status = 'repos-status') regarding:
            -> modifications => 'change_status'
            -> locks => 'lock_agent', 'lock_client'
            of the form
        """
        change_status = None
        lock_agent = None
        lock_client = None
        if (status == 'wc-status' or status == 'repos-status'):
            # missing pristine warning eg is filtered out
            unix_cmd = util.SVN_TEMPLATE_CMD % {
                'subcommand': 'status --xml --show-updates --verbose'} + \
                '"%s" 2> /dev/null' % self.oldcontent_filename
            unix_cmd_list = [unix_cmd]
            retcode, lines = util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())
            if retcode == 0:
                xml_string = ''
                for line in lines:
                    xml_string += line
                dom = parseString(xml_string.encode('utf-8'))
                status_list = dom.getElementsByTagName(status)
                if status_list:
                    change_status = status_list[0].getAttribute("item")
                    lock_list = status_list[0].getElementsByTagName("lock")
                    if lock_list:
                        owner_list = lock_list[0].getElementsByTagName("owner")
                        if owner_list and owner_list[0].childNodes:
                            lock_agent = owner_list[0].childNodes[0].data
                        comment_list = status_list[0].getElementsByTagName("comment")
                        if comment_list and comment_list[0].childNodes:
                            re_client = _('ticket:\d+ \(on behalf of ([^)]+)\)')
                            match = re.search(re_client, comment_list[0].childNodes[0].data)
                            if match:
                                lock_client = match.group(1)
        return {'change_status': change_status,
                'lock_agent': lock_agent,
                'lock_client': lock_client}

    def change_status(self, status):
        """ Return change status:
            -> in the WC if status = 'wc-status'
            -> in the repository if status = 'repos-status'
        """
        return self.status(status)['change_status']

    def lock_status(self):
        " Return lock status in the WC "
        wc_status = self.status('wc-status')
        repos_status = self.status('repos-status')
        return wc_status['lock_agent'] or repos_status['lock_agent']

    def lock(self, ticket_href):
        """ Try to lock the form """
        log_level = self.env.config.get('logging', 'log_level')
        wc_status = self.status('wc-status')
        repos_status = self.status('repos-status')
        if (repos_status['lock_agent'] and
            repos_status['lock_agent'] != 'trac'):
            raise TracError(tag.p("Sorry, the form is already locked by %s. "
                                  "Click"
                                  % repos_status['lock_agent'],
                                  tag.a(" HERE ", href=ticket_href),
                                  "to go back to ticket #%s" % self.id,
                                  class_="message"))
        elif (repos_status['lock_agent'] == 'trac' and
              repos_status['lock_client'] != self.authname):
            raise TracError(tag.p("Sorry, the form is already locked by %s "
                                  "(on behalf of %s). "
                                  "Click"
                                  % (repos_status['lock_agent'],
                                     repos_status['lock_client']),
                                  tag.a(" HERE ", href=ticket_href),
                                  "to go back to ticket #%s" % self.id,
                                  class_="message"))
        elif wc_status['lock_agent'] or repos_status['lock_agent']:
            if log_level == 'INFO':
                syslog.syslog("lock was already set in the working copy or the repository")
        else:
            unix_cmd_list = [util.SVN_TEMPLATE_CMD % {'subcommand': 'lock --force -m "%s" "%s"' % (
                _('ticket:%(id)s (on behalf of %(user)s)', id=str(self.id), user=self.authname),
                self.oldcontent_filename)}]
            retcode, lines = util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())
            if retcode != 0:
                raise TracError('\n'.join(lines))

    def unlock(self):
        """ Try to unlock the form """
        log_level = self.env.config.get('logging', 'log_level')
        wc_status = self.status('wc-status')
        repos_status = self.status('repos-status')
        if (not wc_status['lock_agent'] or
            repos_status['lock_agent'] != 'trac' or
            repos_status['lock_client'] != self.authname or
            not wc_status['lock_client']):
            if log_level == 'INFO':
                syslog.syslog("lock was not set in the working copy or the repository")
        else:
            unix_cmd_list = [util.SVN_TEMPLATE_CMD % {'subcommand': 'unlock --force'} +
                             '"' + self.oldcontent_filename + '"']
            retcode, lines = util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())
            if retcode != 0:
                raise TracError('\n'.join(lines))


class OOoForm(TicketForm):
    """ OpenOffice.org ticket form handling """

    def __init__(self, env, ticket_type, tid, ticket_id, skill, tagname, authname):
        super(OOoForm, self).__init__(env, ticket_type, tid, ticket_id, skill, tagname, authname)
        self.clickonce_app_url = self.env.config.get('artusplugin', 'clickonce_app_url_OpenOffice')
        self.content_path = self.path
        self.content_filename = '%s/%s.%s' % (self.content_path, self.ticket_id,
                                              self.edit_suffix)
        self.webdav_url = self._get_webdav_url()
        self.webdav_fileurl = '%s/%s.%s' % (self.webdav_url, self.ticket_id, self.edit_suffix)
        self.wc_url = self._get_wc_url()
        self.wc_fileurl = '%s/%s.%s' % (self.wc_url, self.ticket_id, self.edit_suffix)
        self.pdf_converter_install_dir = env.config.get('artusplugin',
                                                        'LOo_install_dir')
        self.pdf_converter_port = env.config.get('artusplugin', 'LOo_port')

    def _get_webdav_url(self):
        webdav_url = super(OOoForm, self)._get_webdav_url()
        webdav_url = '%s/t%s' % (webdav_url, self.id)
        return webdav_url

    def _get_wc_url(self):
        wc_url = super(OOoForm, self)._get_wc_url()
        wc_url = '%s/t%s' % (wc_url, self.id)
        return wc_url

    def update(self, ticket, ticket_process_data, mode_list, checked, old_values=None):
        """ Open, modify and save the Ooo form(s) """
        log_level = self.env.config.get('logging', 'log_level')
        if log_level == 'INFO':
            syslog.syslog("%s: Automatic form update !" % ticket_process_data['trac_env_name'])

        # open the old ticket form
        old_doc = load(self.oldcontent_filename)

        if log_level == 'INFO':
            syslog.syslog("%s: old ticket form opened !" % ticket_process_data['trac_env_name'])

        # open the new ticket form
        new_doc = load(self.content_filename)

        if log_level == 'INFO':
            syslog.syslog("%s: new ticket form opened !" % ticket_process_data['trac_env_name'])

        # get the edited form and ticket data (TRAC) for the appropriate ticket type and insert it into the document
        oldform_data, newform_data = self.get_form_data(old_doc, new_doc, mode_list)
        ticket_data = self.get_ticket_data(ticket, ticket_process_data, old_doc, new_doc, oldform_data, newform_data, mode_list, checked)
        self.set_form_data(old_doc, new_doc, ticket_data, mode_list)

        if log_level == 'INFO':
            syslog.syslog("%s: new ticket form edited !" % ticket_process_data['trac_env_name'])

        # save the document(s)
        data_type = mode_list[0]
        if data_type == 'attachments':
            Ooo.doc_save(old_doc, self.oldcontent_filename)

            if log_level == 'INFO':
                syslog.syslog("%s: old ticket form saved !" % ticket_process_data['trac_env_name'])

        Ooo.doc_save(new_doc, self.content_filename)

        if log_level == 'INFO':
            syslog.syslog("%s: new ticket form saved !" % ticket_process_data['trac_env_name'])

    def get_protection_data(self, ticket_status, form_cells, ticket_data, mode, checked):
        """ Determines the status of the Ooo form protections
            according to the ticket status """
        icon_url = "/htdocs/"
        for cell_name in form_cells:
            cell_key = form_cells[cell_name][0]
            cell_status = form_cells[cell_name][1]
            if cell_key not in ticket_data:
                ticket_data[cell_key] = {}
            if cell_status:  # manually updated cell
                if mode == 'help_off':  # cleaning before commit
                    ticket_data[cell_key]['protection'] = "false"
                    ticket_data[cell_key]['background'] = None
                elif ticket_status == cell_status:  # editing allowed
                    ticket_data[cell_key]['protection'] = "false"
                    if checked is True:
                        ticket_data[cell_key]['background'] = icon_url + 'graycheck.png'
                    else:
                        ticket_data[cell_key]['background'] = icon_url + 'greencheck.png'
                else:   # editing not allowed
                    if checked is True:
                        ticket_data[cell_key]['protection'] = "false"  # forced editing
                        ticket_data[cell_key]['background'] = icon_url + 'graycross.png'
                    else:
                        ticket_data[cell_key]['protection'] = "true"   # editing not allowed
                        ticket_data[cell_key]['background'] = icon_url + 'redcross.png'
            else:   # automatically updated cell
                if mode == 'help_off':  # cleaning before commit
                    ticket_data[cell_key]['protection'] = "false"
                    ticket_data[cell_key]['background'] = None
                else:  # form setup before edition
                    ticket_data[cell_key]['protection'] = "true"
                    ticket_data[cell_key]['background'] = icon_url + 'TRAC.jpg'


class OOoEFRForm(OOoForm):
    """ OpenOffice.org EFR form handling """

    HEADER_TABLE_NAME = 'Tableau1'

    FORM_CELLS = [['A1', 'B1', 'C1', 'D1'],
                  ['A2', 'B2', 'C2', 'D2'],
                  ['B3'],
                  ['B4'],
                  ['A5'],
                  ['A6'],
                  ['A7', 'B7'],
                  ['A8', 'B8'],
                  ['A9'],
                  ['A10'],
                  ['A11'],
                  ['A12'],
                  ['A13', 'B13'],
                  ['A14'],
                  ['A15'],
                  ['A16', 'B16'],
                  ['A17'],
                  ['A18', 'B18', 'C18'],
                  ['A19', 'B19', 'C19'],
                  ['A20', 'B20', 'C20'],
                  ['A21', 'B21']]

    TRAC_CELLS = {"A2": ('Program', None, u'Tableau1.A2'),
                  "B2": ('Date', None, u'Tableau1.B2'),
                  "C2": ('Severity', None, u'Tableau1.E2'),
                  "D2": ('TicketId', None, u'Tableau1.G2'),
                  "B4": ('Author', None, u'Tableau1.B4'),
                  "A6": ('Title', None, u'Tableau1.A6'),
                  "A8": ('Version', None, u'Tableau1.A8'),
                  "B8": ('Phase', None, u'Tableau1.F8'),
                  "A10": ('Attachments', None, u'Tableau1.A10'),
                  "B13": ('Description_Contributor', None, u'Tableau1.D13'),
                  "B16": ('Analysis_Contributor', None, u'Tableau1.C16'),
                  "A18": ('Close_Fixed', None, u'Tableau1.A18'),
                  "C18": ('CM_Revision', None, u'Tableau1.H18'),
                  "A19": ('Close_Change_Requested', None, u'Tableau1.A19'),
                  "C19": ('ChangeRequestId', None, u'Tableau1.H19'),
                  "A20": ('Close_Rejected', None, u'Tableau1.A20'),
                  "C20": ('Comment', None, u'Tableau1.H20'),
                  "B21": ('Closure_Decision_Maker', None, u'Tableau1.C21')}

    MODAL_CELLS = {"A12": ('Failure_Description', '01-assigned_for_description', u'Tableau1.A12'),
                   "A15": ('Failure_Analysis', '03-assigned_for_analysis', u'Tableau1.A15')}

    PROTECTED_CELLS = TRAC_CELLS.copy()
    PROTECTED_CELLS.update(MODAL_CELLS)

    TRAC_TEXTUAL_CELLS = {"A2": 'Program',
                          "B2": 'Date',
                          "B4": 'Author',
                          "D2": 'TicketId',
                          "A6": 'Title',
                          "A8": 'Version',
                          "B8": 'Phase',
                          "B13": 'Description_Contributor',
                          "B16": 'Analysis_Contributor',
                          "C18": 'CM_Revision',
                          "C19": 'ChangeRequestId',
                          "C20": 'Comment',
                          "B21": 'Closure_Decision_Maker'}

    USER_TEXTUAL_CELLS = {"A12": 'Failure_Description',
                          "A15": 'Failure_Analysis'}

    TEXTUAL_CELLS = TRAC_TEXTUAL_CELLS.copy()
    TEXTUAL_CELLS.update(USER_TEXTUAL_CELLS)

    ATTACHMENTS_CELL = {"A10": 'Attachments'}

    # The following fields are tested for change
    # Additional data on the change may be added
    # for each field (dictionary key), in the form:
    # (old value, new value)
    # Otherwise 'None' is specified

    TICKET_FIELDS = {'company': None,
                     'summary': None,
                     'keywords': None,
                     'document': None,
                     'phase': None,
                     'authname': None,
                     'status': None,
                     'resolution': None,
                     'submitcomment': None,
                     'severity': None}

    SEVERITY_TYPES = {'Type0': 'Type 0',
                      'Type1A': 'Type 1A',
                      'Type1B': 'Type 1B',
                      'Type2': 'Type 2',
                      'Type3A': 'Type 3A',
                      'Type3B': 'Type 3B',
                      'NotApplicable': 'N/A'}

    RESOLUTION_TYPES = {'CloseEFRfixed': 'fixed',
                        'CloseEFRchangeRequested': 'change requested',
                        'CloseEFRrejected': 'rejected'}

    def __init__(self, env, ticket_type, tid, ticket_id, skill, tagname, authname):
        super(OOoEFRForm, self).__init__(env, ticket_type, tid, ticket_id, skill, tagname, authname)
        self.repo_path = env.config.get('artusplugin', 'efr_forms_dir')
        self.repo_subpath = self.repo_path
        self._set_paths()
        self._set_urls()

    def get_form_data(self, old_doc, new_doc, mode_list):
        """ Read the data from the EFR Ooo form """
        oldform_data = None
        newform_data = None
        data_type = mode_list[0]

        if data_type == 'fields':
            # get old textual data
            oldform_data = {}
            for table in old_doc.getElementsByType(Table):
                if table.getAttribute("name") == 'Tableau1':
                    oldform_data = Ooo.get_text_from_cells(table, self.FORM_CELLS, self.TEXTUAL_CELLS, self.USER_TEXTUAL_CELLS, oldform_data)
                    break

            # get current textual data
            newform_data = {}
            for table in new_doc.getElementsByType(Table):
                if table.getAttribute("name") == 'Tableau1':
                    newform_data = Ooo.get_text_from_cells(table, self.FORM_CELLS, self.TEXTUAL_CELLS, self.USER_TEXTUAL_CELLS, newform_data)
                    break

            # get current graphical data
            for newform in new_doc.getElementsByType(Form):
                Ooo.get_checkbox_choices(newform, newform_data)

        return oldform_data, newform_data

    def get_ticket_data(self, ticket, ticket_process_data, old_doc, new_doc, oldform_data, newform_data, mode_list, checked):
        """ Get the data from the EFR ticket """
        ticket_data = {}
        data_type, help_mode = mode_list

        if data_type == 'fields':

            # get textual data

            ticket_data['Program'] = {}
            ticket_data['Program']['type'] = Ooo.STRING_VALUE_TYPE
            ticket_data['Program']['text'] = self.env.project_name
            ticket_data['Date'] = {}
            ticket_data['Date']['type'] = Ooo.DATE_VALUE_TYPE
            ticket_data['Date']['text'] = ticket.time_created.strftime("%Y-%m-%d")
            ticket_data['Author'] = {}
            ticket_data['Author']['type'] = Ooo.STRING_VALUE_TYPE
            ticket_data['Author']['text'] = ticket['company']
            ticket_data['TicketId'] = {}
            ticket_data['TicketId']['type'] = Ooo.STRING_VALUE_TYPE
            ticket_data['TicketId']['text'] = ticket['summary']
            ticket_data['Title'] = {}
            ticket_data['Title']['type'] = Ooo.STRING_VALUE_TYPE
            ticket_data['Title']['text'] = ticket['keywords']
            ticket_data['Version'] = {}
            ticket_data['Version']['type'] = Ooo.STRING_VALUE_TYPE
            ticket_data['Version']['text'] = ticket['document']
            ticket_data['Phase'] = {}
            ticket_data['Phase']['type'] = Ooo.STRING_VALUE_TYPE
            ticket_data['Phase']['text'] = ticket['phase']
            ticket_data['Description_Contributor'] = {}
            ticket_data['Description_Contributor']['type'] = Ooo.STRING_VALUE_TYPE
            # Only text modifications are traced, so as to filter out non-significant modifications re presentation (eg page breaks)
            if striptags(newform_data['Failure_Description']) != striptags(oldform_data['Failure_Description']):
                ticket_data['Description_Contributor']['text'] = ticket_process_data['authname']
            else:
                ticket_data['Description_Contributor']['text'] = newform_data['Description_Contributor']
            ticket_data['Analysis_Contributor'] = {}
            ticket_data['Analysis_Contributor']['type'] = Ooo.STRING_VALUE_TYPE
            if striptags(newform_data['Failure_Analysis']) != striptags(oldform_data['Failure_Analysis']):
                ticket_data['Analysis_Contributor']['text'] = ticket_process_data['authname']
            else:
                ticket_data['Analysis_Contributor']['text'] = newform_data['Analysis_Contributor']
            ticket_data['CM_Revision'] = {}
            ticket_data['CM_Revision']['type'] = Ooo.STRING_VALUE_TYPE
            ticket_data['ChangeRequestId'] = {}
            ticket_data['ChangeRequestId']['type'] = Ooo.STRING_VALUE_TYPE
            ticket_data['Comment'] = {}
            ticket_data['Comment']['type'] = Ooo.STRING_VALUE_TYPE
            if (ticket['status'] == 'closed' and ticket['resolution'] == 'fixed'):
                match = re.search(r'changeset:(\d+)', ticket['submitcomment'])
                if match:
                    ticket_data['CM_Revision']['text'] = match.group(1)
                else:
                    ticket_data['CM_Revision']['text'] = ''
                ticket_data['ChangeRequestId']['text'] = ''
                ticket_data['Comment']['text'] = ''
            elif ticket['status'] == 'closed' and ticket['resolution'] == 'change requested':
                ticket_data['CM_Revision']['text'] = ''
                match = re.findall(r'ticket:([1-9]\d*)', ticket['submitcomment'])
                ecr_list = None
                for tkt in match:
                    try:
                        ecr_ticket = Ticket(self.env, tkt)
                        if ecr_list:
                            ecr_list += ",\n" + ecr_ticket['summary']
                        else:
                            ecr_list = ecr_ticket['summary']
                    except Exception:
                        continue
                if not ecr_list:
                    ecr_list = ""
                ticket_data['ChangeRequestId']['text'] = ecr_list
                ticket_data['Comment']['text'] = ''
            elif ticket['status'] == 'closed' and ticket['resolution'] == 'rejected':
                ticket_data['CM_Revision']['text'] = ''
                ticket_data['ChangeRequestId']['text'] = ''
                ticket_data['Comment']['text'] = ticket['submitcomment']
            else:
                ticket_data['CM_Revision']['text'] = newform_data['CM_Revision']
                ticket_data['ChangeRequestId']['text'] = newform_data['ChangeRequestId']
                ticket_data['Comment']['text'] = newform_data['Comment']

            # get graphical data

            Ooo.get_ticket_choices(self.SEVERITY_TYPES, ticket['severity'], ticket_data, None)
            ticket_data['Closure_Decision_Maker'] = {}
            ticket_data['Closure_Decision_Maker']['type'] = Ooo.STRING_VALUE_TYPE
            if ticket['status'] == 'closed':
                Ooo.get_ticket_choices(self.RESOLUTION_TYPES, ticket['resolution'], ticket_data, None)
                ticket_data['Closure_Decision_Maker']['text'] = ticket_process_data['authname']
            else:
                Ooo.get_ticket_choices(self.RESOLUTION_TYPES, None, ticket_data, newform_data)
                ticket_data['Closure_Decision_Maker']['text'] = newform_data['Closure_Decision_Maker']

        # data_type == 'attachment' : always done, even in the 'fields' case - useful when attaching and editing simultaneously
        attachment_list = None
        for attachment in Attachment.select(self.env, ticket.resource.realm, ticket.resource.id):
            attachment_name = attachment.filename
            if attachment_list:
                attachment_list = attachment_list + ",\n" + attachment_name
            else:
                attachment_list = attachment_name
        if not attachment_list:
            attachment_list = ""
        ticket_data['Attachments'] = {}
        ticket_data['Attachments']['type'] = Ooo.STRING_VALUE_TYPE
        ticket_data['Attachments']['text'] = attachment_list

        if help_mode:
            # get protection data
            self.get_protection_data(ticket['status'], self.PROTECTED_CELLS, ticket_data, help_mode, checked)

        return ticket_data

    def set_form_data(self, old_doc, new_doc, ticket_data, mode_list):
        """ Write the data to the EFR Ooo form """
        data_type, help_mode = mode_list

        if data_type == 'fields':
            # set meta data
            meta_data = {}
            meta_data['title'] = ticket_data['TicketId']['text']
            meta_data['subject'] = 'Engineering Failure Report'
            meta_data['description'] = ticket_data['Program']['text']
            meta_data['initial creator'] = ticket_data['Author']['text']
            meta_data['creation date'] = ticket_data['Date']['text']
            meta_data['keywords'] = ticket_data['Title']['text']

            Ooo.set_meta_data(new_doc.meta, meta_data)

            # set textual data
            for table in new_doc.getElementsByType(Table):
                if table.getAttribute("name") == 'Tableau1':
                    Ooo.insert_text_into_cells(table, self.FORM_CELLS, self.TRAC_TEXTUAL_CELLS, ticket_data)
                    break

            # set graphical data
            for newform in new_doc.getElementsByType(Form):
                Ooo.set_checkbox_choices(newform, ticket_data)
                break

        # data_type == 'attachment' : always done, even in the 'fields' case - useful when attaching and editing simultaneously
        # set textual data
        for table in old_doc.getElementsByType(Table):
            if table.getAttribute("name") == 'Tableau1':
                Ooo.insert_text_into_cells(table, self.FORM_CELLS, self.ATTACHMENTS_CELL, ticket_data)
                break
        for table in new_doc.getElementsByType(Table):
            if table.getAttribute("name") == 'Tableau1':
                Ooo.insert_text_into_cells(table, self.FORM_CELLS, self.ATTACHMENTS_CELL, ticket_data)
                break

        if help_mode:
            # set protection data
            for table in new_doc.getElementsByType(Table):
                if table.getAttribute("name") == 'Tableau1':
                    Ooo.set_cells_protection(table, self.FORM_CELLS, self.PROTECTED_CELLS, ticket_data)
                    break

    def prepare_pdf(self, tkt_fn, up_id):
        # Update of ticket Id
        doc = load(tkt_fn)
        cell_data = {}
        cell_data['TicketId'] = {}
        cell_data['TicketId']['type'] = Ooo.STRING_VALUE_TYPE
        cell_data['TicketId']['text'] = up_id
        for table in doc.getElementsByType(Table):
            if (table.getAttribute("name") ==
                OOoEFRForm.HEADER_TABLE_NAME):
                Ooo.insert_text_into_cells(
                    table,
                    OOoEFRForm.FORM_CELLS,
                    {"D2": 'TicketId'},
                    cell_data)
                break
        # Empty checkboxes labels (displayed starting with OOo 3.3)
        for frm in doc.getElementsByType(Form):
            for checkbox in frm.getElementsByType(Checkbox):
                checkbox.setAttribute('label', '')
        # Save document
        Ooo.doc_save(doc, tkt_fn)


class OOoECRForm(OOoForm):
    """ OpenOffice.org ECR form handling """

    HEADER_TABLE_NAME = 'Tableau12'

    FORM_CELLS = [['A1', 'B1', 'C1'],
                  ['A2', 'B2', 'C2'],
                  ['A3', 'B3', 'C3'],
                  ['A4', 'B4', 'C4'],
                  ['A5'],
                  ['A6'],
                  ['A7', 'B7'],
                  ['A8', 'B8'],
                  ['A9'],
                  ['A10'],
                  ['A11'],
                  ['A12'],
                  ['A13', 'B13'],
                  ['A14'],
                  ['A15'],
                  ['A16', 'B16'],
                  ['A17'],
                  ['A18', 'B18', 'C18'],
                  ['A19', 'B19'],
                  ['A20'],
                  ['A21'],
                  ['A22'],
                  ['A23'],
                  ['A24'],
                  ['A25'],
                  ['A26'],
                  ['A27'],
                  ['A28'],
                  ['A29'],
                  ['A30'],
                  ['A31', 'B31', 'C31'],
                  ['A32', 'B32', 'C32'],
                  ['A33', 'B33']]

    TRAC_CELLS = {"A2": ('Type', None, u'Tableau12.A2'),
                  "B2": ('Date', None, u'Tableau12.C2'),
                  "C2": ('Parent', None, u'Tableau12.E2'),
                  "A4": ('Program', None, u'Tableau12.A4'),
                  "B4": ('Author', None, u'Tableau12.C4'),
                  "C4": ('TicketId', None, u'Tableau12.E4'),
                  "A6": ('Title', None, u'Tableau12.A6'),
                  "A8": ('Document', None, u'Tableau12.A8'),
                  "B8": ('Applicable', None, u'Tableau12.C8'),
                  "A10": ('Attachments', None, u'Tableau12.A10'),
                  "B13": ('Description_Contributor', None, u'Tableau12.B13'),
                  "B16": ('Analysis_Contributor', None, u'Tableau12.B16'),
                  "A18": ('Implementation_Decision', None, u'Tableau12.A18'),
                  "C18": ('Child', None, u'Tableau12.D18'),
                  "B19": ('Implement_Decision_Maker', None, u'Tableau12.B19'),
                  "A31": ('Close_Fixed', None, u'Tableau12.A31'),
                  "C31": ('CM_Revision', None, u'Tableau12.D31'),
                  "A32": ('Close_Rejected', None, u'Tableau12.A32'),
                  "C32": ('Comment', None, u'Tableau12.D32'),
                  "B33": ('Closure_Decision_Maker', None, u'Tableau12.B33')}

    MODAL_CELLS = {"A12": ('Change_Description', '01-assigned_for_description', u'Tableau12.A12'),
                   "A15": ('Analysis_Resolution', '03-assigned_for_analysis', u'Tableau12.A15'),
                   "A21": ('Specification_Impact', '03-assigned_for_analysis', u'Tableau12.A21'),
                   "A23": ('Design_Impact', '03-assigned_for_analysis', u'Tableau12.A23'),
                   "A25": ('Implementation_Impact', '03-assigned_for_analysis', u'Tableau12.A25'),
                   "A27": ('Test_Impact', '03-assigned_for_analysis', u'Tableau12.A27'),
                   "A29": ('Other_Impact', '03-assigned_for_analysis', u'Tableau12.A29')}

    PROTECTED_CELLS = TRAC_CELLS.copy()
    PROTECTED_CELLS.update(MODAL_CELLS)

    TRAC_TEXTUAL_CELLS = {"A4": 'Program',
                          "B2": 'Date',
                          "B4": 'Author',
                          "C2": 'Parent',
                          "C4": 'TicketId',
                          "A6": 'Title',
                          "A8": 'Document',
                          "B8": 'Applicable',
                          "B13": 'Description_Contributor',
                          "B16": 'Analysis_Contributor',
                          "C18": 'Child',
                          "B19": 'Implement_Decision_Maker',
                          "C31": 'CM_Revision',
                          "C32": 'Comment',
                          "B33": 'Closure_Decision_Maker'}

    USER_TEXTUAL_CELLS = {"A12": 'Change_Description',
                          "A15": 'Analysis_Resolution'}

    TEXTUAL_CELLS = TRAC_TEXTUAL_CELLS.copy()
    TEXTUAL_CELLS.update(USER_TEXTUAL_CELLS)

    ATTACHMENTS_CELL = {"A10": 'Attachments'}

    # The following fields are tested for change
    # Additional data on the change may be added
    # for each field (dictionary key), in the form:
    # (old value, new value)
    # Otherwise 'None' is specified

    TICKET_FIELDS = {'company': None,
                     'summary': None,
                     'keywords': None,
                     'document': None,
                     'milestone': None,
                     'authname': None,
                     'blocking': None,
                     'blockedby': None,
                     'status': None,
                     'resolution': None,
                     'submitcomment': None,
                     'ecrtype': None}

    TICKET_TYPES = {'Evolution': 'Evolution',
                    'ProblemReport': 'Problem Report'}

    RESOLUTION_TYPES = {'CloseECRfixed': 'fixed',
                        'CloseECRrejected': 'rejected'}

    def __init__(self, env, ticket_type, tid, ticket_id, skill, tagname, authname):
        super(OOoECRForm, self).__init__(env, ticket_type, tid, ticket_id, skill, tagname, authname)
        self.repo_path = env.config.get('artusplugin', 'ecr_forms_dir')
        self.repo_subpath = '%s/%s' % (self.repo_path, skill)
        self._set_paths()
        self._set_urls()

    def get_form_data(self, old_doc, new_doc, mode_list):
        """ Read the data from the ECR Ooo form """
        oldform_data = None
        newform_data = None
        data_type = mode_list[0]

        if data_type == 'fields':
            # get old textual data
            oldform_data = {}
            for table in old_doc.getElementsByType(Table):
                if table.getAttribute("name") == 'Tableau12':
                    oldform_data = Ooo.get_text_from_cells(table, self.FORM_CELLS, self.TEXTUAL_CELLS, self.USER_TEXTUAL_CELLS, oldform_data)
                    break

            # get current textual data
            newform_data = {}
            for table in new_doc.getElementsByType(Table):
                if table.getAttribute("name") == 'Tableau12':
                    newform_data = Ooo.get_text_from_cells(table, self.FORM_CELLS, self.TEXTUAL_CELLS, self.USER_TEXTUAL_CELLS, newform_data)
                    break

            # get current graphical data
            for newform in new_doc.getElementsByType(Form):
                Ooo.get_checkbox_choices(newform, newform_data)

        return oldform_data, newform_data

    def get_ticket_data(self, ticket, ticket_process_data, old_doc, new_doc, oldform_data, newform_data, mode_list, checked):
        """ Get the data from the ECR ticket """
        ticket_data = {}
        data_type, help_mode = mode_list

        if data_type == 'fields':
            # get textual data

            ticket_data['Date'] = {}
            ticket_data['Date']['type'] = Ooo.DATE_VALUE_TYPE
            ticket_data['Date']['text'] = ticket.time_created.strftime("%Y-%m-%d")
            parent_list = None
            parent_field_name = 'blocking'
            for parent_id in ticket[parent_field_name].split(','):
                if parent_id == '':
                    continue
                else:
                    try:
                        parent_ticket = Ticket(self.env, int(parent_id))
                        if parent_list:
                            parent_list += ",\n" + parent_ticket['summary']
                        else:
                            parent_list = parent_ticket['summary']
                    except Exception:
                        continue
            if not parent_list:
                parent_list = ""
            ticket_data['Parent'] = {}
            ticket_data['Parent']['type'] = Ooo.STRING_VALUE_TYPE
            ticket_data['Parent']['text'] = parent_list
            ticket_data['Program'] = {}
            ticket_data['Program']['type'] = Ooo.STRING_VALUE_TYPE
            if self.env.project_name.endswith(' SW'):
                ticket_data['Program']['text'] = self.env.project_name[:-3]
            else:
                ticket_data['Program']['text'] = self.env.project_name
            ticket_data['Author'] = {}
            ticket_data['Author']['type'] = Ooo.STRING_VALUE_TYPE
            ticket_data['Author']['text'] = ticket['company']
            ticket_data['TicketId'] = {}
            ticket_data['TicketId']['type'] = Ooo.STRING_VALUE_TYPE
            ticket_data['TicketId']['text'] = ticket['summary']
            ticket_data['Title'] = {}
            ticket_data['Title']['type'] = Ooo.STRING_VALUE_TYPE
            ticket_data['Title']['text'] = ticket['keywords']
            ticket_data['Document'] = {}
            ticket_data['Document']['type'] = Ooo.STRING_VALUE_TYPE
            ticket_data['Document']['text'] = ticket['document']
            ticket_data['Applicable'] = {}
            ticket_data['Applicable']['type'] = Ooo.STRING_VALUE_TYPE
            ticket_data['Applicable']['text'] = ticket['milestone']
            ticket_data['Description_Contributor'] = {}
            ticket_data['Description_Contributor']['type'] = Ooo.STRING_VALUE_TYPE
            # Only text modifications are traced, so as to filter out non-significant modifications re presentation (eg page breaks)
            if striptags(newform_data['Change_Description']) != striptags(oldform_data['Change_Description']):
                ticket_data['Description_Contributor']['text'] = ticket_process_data['authname']
            else:
                ticket_data['Description_Contributor']['text'] = newform_data['Description_Contributor']
            ticket_data['Analysis_Contributor'] = {}
            ticket_data['Analysis_Contributor']['type'] = Ooo.STRING_VALUE_TYPE
            if striptags(newform_data['Analysis_Resolution']) != striptags(oldform_data['Analysis_Resolution']):
                ticket_data['Analysis_Contributor']['text'] = ticket_process_data['authname']
            else:
                ticket_data['Analysis_Contributor']['text'] = newform_data['Analysis_Contributor']
            child_list = None
            for child_id in ticket['blockedby'].split(','):
                if child_id == '':
                    continue
                else:
                    try:
                        child_ticket = Ticket(self.env, int(child_id))
                        if child_list:
                            child_list = child_list + ",\n" + child_ticket['summary']
                        else:
                            child_list = child_ticket['summary']
                    except Exception:
                        continue
            if not child_list:
                child_list = ""
            ticket_data['Child'] = {}
            ticket_data['Child']['type'] = Ooo.STRING_VALUE_TYPE
            ticket_data['Child']['text'] = child_list
            ticket_data['CM_Revision'] = {}
            ticket_data['CM_Revision']['type'] = Ooo.STRING_VALUE_TYPE
            ticket_data['Comment'] = {}
            ticket_data['Comment']['type'] = Ooo.STRING_VALUE_TYPE
            if ticket['status'] == 'closed' and ticket['resolution'] == 'fixed':
                match = re.search(r'changeset:(\d+)', ticket['submitcomment'])
                if match:
                    ticket_data['CM_Revision']['text'] = match.group(1)
                else:
                    ticket_data['CM_Revision']['text'] = ''
                ticket_data['Comment']['text'] = ''
            elif ticket['status'] == 'closed' and ticket['resolution'] == 'rejected':
                ticket_data['CM_Revision']['text'] = ''
                ticket_data['Comment']['text'] = ticket['submitcomment']
            else:
                ticket_data['CM_Revision']['text'] = newform_data['CM_Revision']
                ticket_data['Comment']['text'] = newform_data['Comment']

            # get graphical data
            Ooo.get_ticket_choices(self.TICKET_TYPES, ticket['ecrtype'], ticket_data, None)
            ticket_data['Implement_Decision_Maker'] = {}
            ticket_data['Implement_Decision_Maker']['type'] = Ooo.STRING_VALUE_TYPE
            if newform_data['Implement'] == Ooo.UNCHECKED and ticket['status'] == '05-assigned_for_implementation':
                ticket_data['Implement'] = Ooo.CHECKED
                ticket_data['Implement_Decision_Maker']['text'] = ticket_process_data['authname']
            else:
                ticket_data['Implement'] = newform_data['Implement']
                ticket_data['Implement_Decision_Maker']['text'] = newform_data['Implement_Decision_Maker']
            ticket_data['Closure_Decision_Maker'] = {}
            ticket_data['Closure_Decision_Maker']['type'] = Ooo.STRING_VALUE_TYPE
            if ticket['status'] == 'closed':
                Ooo.get_ticket_choices(self.RESOLUTION_TYPES, ticket['resolution'], ticket_data, None)
                ticket_data['Closure_Decision_Maker']['text'] = ticket_process_data['authname']
            else:
                Ooo.get_ticket_choices(self.RESOLUTION_TYPES, None, ticket_data, newform_data)
                ticket_data['Closure_Decision_Maker']['text'] = newform_data['Closure_Decision_Maker']

        # data_type == 'attachment' : always done, even in the 'fields' case - useful when attaching and editing simultaneously
        attachment_list = None
        for attachment in Attachment.select(self.env, ticket.resource.realm, ticket.resource.id):
            attachment_name = attachment.filename
            if attachment_list:
                attachment_list += ",\n" + attachment_name
            else:
                attachment_list = attachment_name
        if not attachment_list:
            attachment_list = ""
        ticket_data['Attachments'] = {}
        ticket_data['Attachments']['type'] = Ooo.STRING_VALUE_TYPE
        ticket_data['Attachments']['text'] = attachment_list

        if help_mode:
            # get protection data
            self.get_protection_data(ticket['status'], self.PROTECTED_CELLS, ticket_data, help_mode, checked)

        return ticket_data

    def set_form_data(self, old_doc, new_doc, ticket_data, mode_list):
        """ Write the data to the ECR Ooo form """
        data_type, help_mode = mode_list

        if data_type == 'fields':
            # set meta data
            meta_data = {}
            meta_data['title'] = ticket_data['TicketId']['text']
            meta_data['subject'] = 'Engineering Change Request'
            meta_data['description'] = ticket_data['Program']['text']
            meta_data['initial creator'] = ticket_data['Author']['text']
            meta_data['creation date'] = ticket_data['Date']['text']
            meta_data['keywords'] = ticket_data['Title']['text']

            Ooo.set_meta_data(new_doc.meta, meta_data)

            # set textual data
            for table in new_doc.getElementsByType(Table):
                if table.getAttribute("name") == 'Tableau12':
                    Ooo.insert_text_into_cells(table, self.FORM_CELLS, self.TRAC_TEXTUAL_CELLS, ticket_data)
                    break

            # set graphical data
            for newform in new_doc.getElementsByType(Form):
                Ooo.set_checkbox_choices(newform, ticket_data)
                break

        # data_type == 'attachment' : always done, even in the 'fields' case - useful when attaching and editing simultaneously
        # set textual data
        for table in old_doc.getElementsByType(Table):
            if table.getAttribute("name") == 'Tableau12':
                Ooo.insert_text_into_cells(table, self.FORM_CELLS, self.ATTACHMENTS_CELL, ticket_data)
                break
        for table in new_doc.getElementsByType(Table):
            if table.getAttribute("name") == 'Tableau12':
                Ooo.insert_text_into_cells(table, self.FORM_CELLS, self.ATTACHMENTS_CELL, ticket_data)
                break

        if help_mode:
            # set protection data
            for table in new_doc.getElementsByType(Table):
                if table.getAttribute("name") == 'Tableau12':
                    Ooo.set_cells_protection(table, self.FORM_CELLS, self.PROTECTED_CELLS, ticket_data)
                    break

    def prepare_pdf(self, tkt_fn, up_id):
        # Update of ticket Id
        doc = load(tkt_fn)
        cell_data = {}
        cell_data['TicketId'] = {}
        cell_data['TicketId']['type'] = Ooo.STRING_VALUE_TYPE
        cell_data['TicketId']['text'] = up_id
        for table in doc.getElementsByType(Table):
            if (table.getAttribute("name") ==
                OOoECRForm.HEADER_TABLE_NAME):
                Ooo.insert_text_into_cells(
                    table,
                    OOoECRForm.FORM_CELLS,
                    {"C4": 'TicketId'},
                    cell_data)
                break
        # Empty checkboxes labels (displayed starting with OOo 3.3)
        for frm in doc.getElementsByType(Form):
            for checkbox in frm.getElementsByType(Checkbox):
                checkbox.setAttribute('label', '')
        # Save document
        Ooo.doc_save(doc, tkt_fn)


class OOoRFForm(OOoForm):
    """ OpenOffice.org RF form handling """

    HEADER_TABLE_NAME = 'Tableau9'

    TABLE1_CELLS = [['A1', 'B1', 'C1'],
                    ['A2', 'B2', 'C2', 'D2', 'E2'],
                    ['A3', 'B3', 'C3', 'D3', 'E3'],
                    ['A4', 'B4', 'C4', 'D4', 'E4'],
                    ['A5', 'B5', 'C5', 'D5', 'E5'],
                    ['A6', 'B6', 'C6', 'D6'],
                    ['A7', 'B7', 'C7', 'D7', 'E7']]

    TABLE2_BODY_CELLS = [['A3', 'B3', 'C3', 'D3', 'E3', 'F3', 'G3']]

    TRAC_CELLS = {"B2": ('Program', None, u'Tableau9.B2'),
                  "E2": ('RFReference', None, u'Tableau9.E2'),
                  "E3": ('RFMilestone', None, u'Tableau9.E3'),
                  "B4": ('DocReference', None, u'Tableau9.B4'),
                  "E4": ('RFReader', None, u'Tableau9.E4'),
                  "B5": ('DocVersion', None, u'Tableau9.B5'),
                  "E5": ('RFDate', None, u'Tableau9.E5'),
                  "B6": ('DocStatus', None, u'Tableau9.B6'),
                  "B7": ('DocRevision', None, u'Tableau9.B7'),
                  "E7": ('CHKLSTReference', None, u'Tableau9.E7')}

    AUTHOR_CELLS = {"B3": ('Chapter_Page', '01-assigned_for_description', u'Tableau11.B3'),
                    "C3": ('Remark', '01-assigned_for_description', u'Tableau11.C3'),
                    "D3": ('Criticality', '01-assigned_for_description', u'Tableau11.D3')}

    PROTECTED_CELLS = {"B3": ('Chapter_Page', '01-assigned_for_description', u'Tableau11.B3'),
                       "C3": ('Remark', '01-assigned_for_description', u'Tableau11.C3'),
                       "D3": ('Criticality', '01-assigned_for_description', u'Tableau11.D3'),
                       "E3": ('Decision', '03-assigned_for_analysis', u'Tableau11.E3'),
                       "F3": ('Comment', '03-assigned_for_analysis', u'Tableau11.F3'),
                       "G3": ('CM_Revision', '06-implemented', u'Tableau11.G3')}

    TEXTUAL_CELLS = {"B2": 'Program',
                     "B3": 'DocTitle',
                     "B4": 'DocReference',
                     "B5": 'DocVersion',
                     "B6": 'DocStatus',
                     "B7": 'DocRevision',
                     "E2": 'RFReference',
                     "E3": 'RFMilestone',
                     "E4": 'RFReader',
                     "E5": 'RFDate'}

    ATTACHMENTS_CELL = {"E7": 'CHKLSTReference'}

    # The following fields are tested for change
    # Additional data on the change may be added
    # for each field (dictionary key), in the form:
    # (old value, new value)
    # Otherwise 'None' is specified

    TICKET_FIELDS = {'document': None,
                     'summary': None,
                     'milestone': None,
                     'authname': None,
                     'owner': None,
                     'status': None}

    def __init__(self, env, ticket_type, tid, ticket_id, skill, tagname, authname):
        super(OOoRFForm, self).__init__(env, ticket_type, tid, ticket_id, skill, tagname, authname)
        self.repo_path = env.config.get('artusplugin', 'rf_forms_dir')
        self.repo_subpath = '%s/%s' % (self.repo_path, tagname)
        self._set_paths()
        self._set_urls()

    def get_form_data(self, old_doc, new_doc, mode_list):
        """ Read the data from the RF Ooo form """
        oldform_data = None
        newform_data = None
        data_type = mode_list[0]

        if data_type == 'fields':
            # get old textual data
            oldform_data = {}
            for table in old_doc.getElementsByType(Table):
                if table.getAttribute("name") == 'Tableau9':
                    oldform_data = Ooo.get_text_from_cells(table, self.TABLE1_CELLS, self.TEXTUAL_CELLS, {}, oldform_data)
                elif table.getAttribute("name") == 'Tableau11':
                    rf_old_form_protected_cells = Ooo.get_col_cells(table, self.PROTECTED_CELLS, 'text')
                    rf_old_form_table2_cells = Ooo.get_rows_list(table, self.TABLE2_BODY_CELLS)
                    oldform_data = Ooo.get_text_from_cells(table, rf_old_form_table2_cells, rf_old_form_protected_cells, rf_old_form_protected_cells, oldform_data)

            # get current textual data
            newform_data = {}
            for table in new_doc.getElementsByType(Table):
                if table.getAttribute("name") == 'Tableau9':
                    newform_data = Ooo.get_text_from_cells(table, self.TABLE1_CELLS, self.TEXTUAL_CELLS, {}, newform_data)
                elif table.getAttribute("name") == 'Tableau11':
                    rf_new_form_protected_cells = Ooo.get_col_cells(table, self.PROTECTED_CELLS, 'text')
                    rf_new_form_table2_cells = Ooo.get_rows_list(table, self.TABLE2_BODY_CELLS)
                    newform_data = Ooo.get_text_from_cells(table, rf_new_form_table2_cells, rf_new_form_protected_cells, rf_new_form_protected_cells, newform_data)

        return oldform_data, newform_data

    def get_ticket_data(self, ticket, ticket_process_data, old_doc, new_doc, oldform_data, newform_data, mode_list, checked):
        """ Get the data from the RF ticket """
        ticket_data = {}
        data_type, help_mode = mode_list

        if data_type == 'fields':
            # get textual data
            ticket_data['Program'] = {}
            ticket_data['Program']['type'] = Ooo.STRING_VALUE_TYPE
            if self.env.project_name.endswith(' SW'):
                ticket_data['Program']['text'] = self.env.project_name[:-3]
            else:
                ticket_data['Program']['text'] = self.env.project_name
            ticket_data['DocReference'] = {}
            ticket_data['DocReference']['type'] = Ooo.STRING_VALUE_TYPE
            ticket_data['DocVersion'] = {}
            ticket_data['DocVersion']['type'] = Ooo.STRING_VALUE_TYPE
            ticket_data['DocStatus'] = {}
            ticket_data['DocStatus']['type'] = Ooo.STRING_VALUE_TYPE

            (ticket_data['DocReference']['text'],
             ticket_data['DocVersion']['text'],
             ticket_data['DocStatus']['text']) = NamingRule.split_version_tag(
                self.env,
                ticket['document'],
                self.program_name)

            # The doc title is set by hand for now
            ticket_data['DocTitle'] = {}
            ticket_data['DocTitle']['type'] = Ooo.STRING_VALUE_TYPE
            ticket_data['DocTitle']['text'] = newform_data['DocTitle']

            # Get the document subversion revision
            ticket_data['DocRevision'] = {}
            ticket_data['DocRevision']['type'] = Ooo.STRING_VALUE_TYPE
            trunkurl, trunkrev = util.get_trunk_url_rev_from_tag(self.env, ticket)

            # We want the nearest revision to trunkrev
            trunkrev = util.get_last_path_rev_author(self.env, trunkurl, trunkrev, resync=False)[2]

            if newform_data['DocRevision'] == "":
                ticket_data['DocRevision']['text'] = trunkrev
            else:
                ticket_data['DocRevision']['text'] = newform_data['DocRevision']

            ticket_data['RFReference'] = {}
            ticket_data['RFReference']['type'] = Ooo.STRING_VALUE_TYPE
            ticket_data['RFReference']['text'] = ticket['summary']
            ticket_data['RFMilestone'] = {}
            ticket_data['RFMilestone']['type'] = Ooo.STRING_VALUE_TYPE
            ticket_data['RFMilestone']['text'] = ticket['milestone']

            # The protected cells are set by hand

            # get protected cells for new form
            for table in new_doc.getElementsByType(Table):
                if table.getAttribute("name") == 'Tableau11':
                    rf_new_form_author_cells = Ooo.get_col_cells(table, self.AUTHOR_CELLS, 'text')
                    break

            # get protected cells for old form
            for table in old_doc.getElementsByType(Table):
                if table.getAttribute("name") == 'Tableau11':
                    rf_old_form_author_cells = Ooo.get_col_cells(table, self.AUTHOR_CELLS, 'text')
                    break

            ticket_data['RFReader'] = {}
            ticket_data['RFReader']['type'] = Ooo.STRING_VALUE_TYPE
            if rf_new_form_author_cells == rf_old_form_author_cells:
                for cell in rf_new_form_author_cells:
                    cell_key = rf_new_form_author_cells[cell]
                    # Only text modifications are traced, so as to filter out
                    # non-significant modifications re presentation
                    # (e.g. page breaks)
                    if striptags(newform_data[cell_key]) != striptags(oldform_data[cell_key]):
                        ticket_data['RFReader']['text'] = ticket_process_data['authname']
                        break
                else:
                    if newform_data['RFReader'] == '':
                        ticket_data['RFReader']['text'] = ticket['owner']
                    else:
                        ticket_data['RFReader']['text'] = newform_data['RFReader']
            else:
                ticket_data['RFReader']['text'] = ticket_process_data['authname']

            ticket_data['RFDate'] = {}
            ticket_data['RFDate']['type'] = Ooo.DATE_VALUE_TYPE
            ticket_data['RFDate']['text'] = ticket.time_created.strftime("%Y-%m-%d")

        # data_type == 'attachment' : always done, even in the 'fields' case - useful when attaching and editing simultaneously
        chklst_name = ''
        for attachment in Attachment.select(self.env, ticket.resource.realm, ticket.resource.id):
            attachment_name = attachment.filename
            if attachment_name.startswith('CHKLST_'):
                chklst_name = attachment_name
                break
        ticket_data['CHKLSTReference'] = {}
        ticket_data['CHKLSTReference']['type'] = Ooo.STRING_VALUE_TYPE
        ticket_data['CHKLSTReference']['text'] = chklst_name

        if help_mode:
            # get protected cells for new form
            for table in new_doc.getElementsByType(Table):
                if table.getAttribute("name") == 'Tableau11':
                    rf_new_form_protected_cells = Ooo.get_col_cells(table, self.PROTECTED_CELLS, 'protection')
                    break

            # get protection data
            self.get_protection_data(ticket['status'], self.TRAC_CELLS, ticket_data, help_mode, checked)
            self.get_protection_data(ticket['status'], rf_new_form_protected_cells, ticket_data, help_mode, checked)

        return ticket_data

    def set_form_data(self, old_doc, new_doc, ticket_data, mode_list):
        """ Write the data to the RF Ooo form """
        data_type, help_mode = mode_list

        if data_type == 'fields':
            # set meta data
            meta_data = {}
            meta_data['title'] = ticket_data['RFReference']['text']
            meta_data['subject'] = 'Reading Form'
            meta_data['description'] = ticket_data['Program']['text']
            meta_data['initial creator'] = ticket_data['RFReader']['text']
            meta_data['creation date'] = ticket_data['RFDate']['text']
            meta_data['keywords'] = ''

            Ooo.set_meta_data(new_doc.meta, meta_data)

            # set textual data
            for table in new_doc.getElementsByType(Table):
                if table.getAttribute("name") == 'Tableau9':
                    Ooo.insert_text_into_cells(table, self.TABLE1_CELLS, self.TEXTUAL_CELLS, ticket_data)
                    break

        # data_type == 'attachment' : always done, even in the 'fields' case - useful when attaching and editing simultaneously
        # set textual data
        for table in old_doc.getElementsByType(Table):
            if table.getAttribute("name") == 'Tableau9':
                Ooo.insert_text_into_cells(table, self.TABLE1_CELLS, self.ATTACHMENTS_CELL, ticket_data)
                break
        for table in new_doc.getElementsByType(Table):
            if table.getAttribute("name") == 'Tableau9':
                Ooo.insert_text_into_cells(table, self.TABLE1_CELLS, self.ATTACHMENTS_CELL, ticket_data)
                break

        if help_mode:
            # get protected cells for new form and set protection data
            for table in new_doc.getElementsByType(Table):
                if table.getAttribute("name") == 'Tableau9':
                    Ooo.set_cells_protection(table, self.TABLE1_CELLS, self.TRAC_CELLS, ticket_data)
                elif table.getAttribute("name") == 'Tableau11':
                    rf_new_form_protected_cells = Ooo.get_col_cells(table, self.PROTECTED_CELLS, 'protection')
                    rf_new_form_table2_cells = Ooo.get_rows_list(table, self.TABLE2_BODY_CELLS)
                    Ooo.set_cells_protection(table, rf_new_form_table2_cells, rf_new_form_protected_cells, ticket_data)
        return

    def prepare_pdf(self, tkt_fn, up_id):
        # Update of ticket Id
        doc = load(tkt_fn)
        cell_data = {}
        cell_data['RFReference'] = {}
        cell_data['RFReference']['type'] = Ooo.STRING_VALUE_TYPE
        cell_data['RFReference']['text'] = up_id
        for table in doc.getElementsByType(Table):
            if (table.getAttribute("name") ==
                OOoRFForm.HEADER_TABLE_NAME):
                Ooo.insert_text_into_cells(
                    table,
                    OOoRFForm.TABLE1_CELLS,
                    {"E2": 'RFReference'},
                    cell_data)
                break
        # Save document
        Ooo.doc_save(doc, tkt_fn)


class MSOForm(TicketForm):
    """ MS Office ticket form handling """

    def __init__(self, env, ticket_type, tid, ticket_id, skill, tagname, authname):
        super(MSOForm, self).__init__(env, ticket_type, tid, ticket_id, skill, tagname, authname)
        self.clickonce_app_url = self.env.config.get('artusplugin', 'clickonce_app_url_MS_Office')
        self.content_path = '%s/webdav' % self.path
        self.content_filename = '%s/%s.%s' % (self.content_path, self.ticket_id,
                                              self.edit_suffix)
        self.copyform_filename = '%s/trac_data.xml' % self.copycontent_path
        self.copyform_data = self._get_form_data(self.copyform_filename)
        self.form_filename = '%s/trac_data.xml' % self.path
        self.form_data = self._get_form_data(self.form_filename)
        self.host = util.get_hostname(env)
        self.webdav_url = self._get_webdav_url()
        self.webdav_fileurl = '%s/%s.%s' % (self.webdav_url, self.ticket_id, self.edit_suffix)
        self.wc_url = self._get_wc_url()
        self.wc_fileurl = '%s/%s.%s' % (self.wc_url, self.ticket_id, self.edit_suffix)
        self.pdf_converter_install_dir = env.config.get('artusplugin',
                                                        'LOo_install_dir')
        self.pdf_converter_port = self.env.config.get("artusplugin", "LOo_port")
        self.schemas_url = self.env.config.get("artusplugin", "schemas_url")
        self.schemas_url_legacy_list = [url.strip() for url in env.config.get("artusplugin", "schemas_url_legacy_list").split(',')]
        self.ns0 = "%s/%s" % (self.schemas_url, self.ticket_type)
        self.ns0_legacy_list = ["%s/%s" % (url, ticket_type) for url in self.schemas_url_legacy_list]
        self.root_tag = "{%s}%s" % (self.ns0, self.ticket_type)
        self.root_tag_legacy_list = ["{%s}%s" % (ns0, ticket_type) for ns0 in self.ns0_legacy_list]

    def _get_form_data(self, filename):
        return FormData.get_data(self.env, self.ticket_type)(self.env, filename)

    def _get_webdav_url(self):
        webdav_url = super(MSOForm, self)._get_webdav_url()
        webdav_url = '%s/t%s/webdav' % (webdav_url, self.id)
        return webdav_url

    def _get_wc_url(self):
        wc_url = super(MSOForm, self)._get_wc_url()
        wc_url = '%s/t%s/webdav' % (wc_url, self.id)
        return wc_url

    def _unzip(self, filename, tmpdir):
        """ Open docm/xlsm """
        with zipfile.ZipFile(filename, "r") as zip_ref:
            zip_ref.extractall(tmpdir)

    def _zip(self, filename, tmpdir):
        """ Close docm/xlsm """
        util.zip_dir(tmpdir, filename)

    def get_customxml_filepath(self, dir):
        item_dirpath = '%s/customXml' % dir
        for item_filename in fnmatch.filter(os.listdir(item_dirpath), 'item[0-9].xml'):
            item_filepath = "%s/%s" % (item_dirpath, item_filename)
            customxml_root_elt = ElementTree.parse(item_filepath).getroot()
            if customxml_root_elt.tag in [self.root_tag] + self.root_tag_legacy_list:
                return item_filepath

    def get_creation_datetime(self):
        with TemporaryDirectory() as tmpdir:
            self._unzip(self.content_filename, tmpdir)
            customxml_dom = parse(self.get_customxml_filepath(tmpdir))
            lst = customxml_dom.getElementsByTagName("Template")
            if lst:
                elt = lst[0]
                return elt.getAttribute("CreationDateTime")
            else:
                return None

    def get_modification_datetime(self):
        with TemporaryDirectory() as tmpdir:
            self._unzip(self.content_filename, tmpdir)
            customxml_dom = parse(self.get_customxml_filepath(tmpdir))
            lst = customxml_dom.getElementsByTagName("Template")
            if lst:
                elt = lst[0]
                return elt.getAttribute("ModificationDateTime")
            else:
                return None

    def get_reference(self):
        with TemporaryDirectory() as tmpdir:
            self._unzip(self.content_filename, tmpdir)
            customxml_dom = parse(self.get_customxml_filepath(tmpdir))
            try:
                elt = customxml_dom.getElementsByTagName("TemplateRef")[0]
                return elt.firstChild.nodeValue
            except IndexError:
                return None

    def get_vba_hash(self):
        hash_md5 = hashlib.md5()
        with TemporaryDirectory() as tmpdir:
            self._unzip(self.content_filename, tmpdir)
            vba_filename = '%s/%s' % (tmpdir, self.vbaprojectfile)
            with open(vba_filename, "rb") as f:
                for chunk in iter(lambda: f.read(4096), b""):
                    hash_md5.update(chunk)
        return hash_md5.hexdigest()

    def update_modification_datetime(self, tft, template_path):
        tmpl_mt = tft.get_modification_datetime(template_path)
        if tmpl_mt:
            with TemporaryDirectory() as tmpdir:
                self._unzip(self.content_filename, tmpdir)
                customxml_filename = self.get_customxml_filepath(tmpdir)
                customxml_tree = ElementTree.parse(customxml_filename)
                customxml_tmpl = customxml_tree.find('{%s}Template' % self.ns0)
                customxml_tmpl.set('ModificationDateTime', tmpl_mt)
                customxml_tree.write(customxml_filename)
                self._zip(self.content_filename, tmpdir)
                syslog.syslog("%s(%s): Form modification datetime updated from template - ticket %s (%s)" %
                              (self.trac_env_name, self.authname, self.ticket_id, self.ticket_type))

    def upgrade(self, tft):
        # Get the form template reference (including edition) - exception means legacy
        form_template_ref = self.get_reference()
        if form_template_ref:
            legacy = False
        else:
            legacy = True
        if tft.repo_form:
            template_path = tft.source_name
        else:
            # Get the standard template filename (without edition)
            template_filepath = "%s/%s" % (tft.env_templates_dir, tft.name)
            if form_template_ref:
                # Get the standard template reference (without edition)
                template_ref = os.path.splitext(tft.name)[0]
                # Get the current edition ultimate pointed to path, where all editions are located
                template_path = os.path.realpath(template_filepath)
                # Get the compatible template for providing an upgraded VBA code
                template_path = re.sub(r"%s_E[0-9]+" % template_ref, form_template_ref, template_path)
            else:
                # Get the current edition symbolic link target path
                template_path = os.readlink(template_filepath)
                # Get the default edition template path for providing an upgraded VBA code
                template_path = re.sub(r"current", r"default", template_path)
                # Get the default edition ultimate pointed to path
                template_path = os.path.realpath(template_path)
            if not os.path.exists(template_path):
                # No upgrade
                return

        form_mt = self.get_modification_datetime()
        tmpl_mt = tft.get_modification_datetime(template_path)
        if not form_mt or (tmpl_mt and tmpl_mt > form_mt):
            if legacy:
                self.upgrade_vba_legacy(tft, template_path)
            else:
                self.upgrade_vba(tft, template_path)
            if not form_mt:
                self.upgrade_customxml(tft, template_path)
            else:
                self.update_modification_datetime(tft, template_path)
            self.upgrade_databinding()

    def upgrade_vba_legacy(self, tft, template_path):
        with TemporaryDirectory() as tft_tmpdir, TemporaryDirectory() as tf_tmpdir:
            tft._unzip(tft.get_cache_name(tft_tmpdir, template_path), tft_tmpdir)
            self._unzip(self.content_filename, tf_tmpdir)
            vbaprojectfiles= [('%s/%s' % (tft_tmpdir, self.vbaprojectfile),
                               '%s/%s' % (tf_tmpdir, self.vbaprojectfile)),
                              ('%s/%s' % (tft_tmpdir, self.vbasignaturefile),
                               '%s/%s' % (tf_tmpdir, self.vbasignaturefile))]
            atime = mtime = (datetime(1980, 1, 1) - datetime(1970, 1, 1)).total_seconds()
            for srcfile, destfile in vbaprojectfiles:
                if os.path.exists(srcfile) and os.stat(srcfile).st_size != 0:
                    shutil.copy(srcfile, destfile)
                    os.utime(destfile, (atime, mtime))
            self._zip(self.content_filename, tf_tmpdir)
            syslog.syslog("%s(%s): VBA project upgraded from template - ticket %s (%s)" %
                          (self.trac_env_name, self.authname, self.id, self.ticket_type))

    def upgrade_customxml(self, tft, template_path):
        with TemporaryDirectory() as tft_tmpdir, TemporaryDirectory() as tf_tmpdir:
            tft._unzip(tft.get_cache_name(tft_tmpdir, template_path), tft_tmpdir)
            self._unzip(self.content_filename, tf_tmpdir)
            custom_xml_filename_src = self.get_customxml_filepath(tft_tmpdir)
            custom_xml_filename_dst = self.get_customxml_filepath(tf_tmpdir)
            shutil.copy(custom_xml_filename_src, custom_xml_filename_dst)
            self._zip(self.content_filename, tf_tmpdir)
            syslog.syslog("%s(%s): CustomXML upgraded from template - ticket %s (%s)" %
                          (self.trac_env_name, self.authname, self.id, self.ticket_type))

    def upgrade_databinding(self):
        with TemporaryDirectory() as tmpdir:
            self._unzip(self.content_filename, tmpdir)
            for xml_file in ['document.xml', 'footer1.xml']:
                xml_filepath = '%s/word/%s' % (tmpdir, xml_file)
                xml_tree = ElementTree.parse(xml_filepath)
                xml_root = xml_tree.getroot()
                xml_ns = xml_root.nsmap['w']
                for data_binding in xml_tree.findall('.//w:dataBinding', xml_root.nsmap):
                    prefix_mappings = data_binding.get('{%s}prefixMappings' % xml_ns)
                    for schemas_url_legacy in self.schemas_url_legacy_list:
                        if schemas_url_legacy in prefix_mappings:
                            prefix_mappings = prefix_mappings.replace(schemas_url_legacy, self.schemas_url)
                            break
                    data_binding.set('{%s}prefixMappings' % xml_ns, prefix_mappings)
                xml_tree.write(xml_filepath)
            self._zip(self.content_filename, tmpdir)
            syslog.syslog("%s(%s): Databinding upgraded - ticket %s (%s)" %
                          (self.trac_env_name, self.authname, self.id, self.ticket_type))

    def upgrade_vba(self, tft, template_path):
        with TemporaryDirectory() as tft_tmpdir, TemporaryDirectory() as tf_tmpdir:
            tft._unzip(tft.get_cache_name(tft_tmpdir, template_path), tft_tmpdir)
            self._unzip(self.content_filename, tf_tmpdir)
            vbaprojectfiles= [('%s/%s' % (tft_tmpdir, self.vbaprojectfile),
                               '%s/%s' % (tf_tmpdir, self.vbaprojectfile)),
                              ('%s/%s' % (tft_tmpdir, self.vbasignaturefile),
                               '%s/%s' % (tf_tmpdir, self.vbasignaturefile)),
                              ('%s/%s' % (tft_tmpdir, self.vbadatafile),
                               '%s/%s' % (tf_tmpdir, self.vbadatafile))]
            atime = mtime = (datetime(1980, 1, 1) - datetime(1970, 1, 1)).total_seconds()
            for srcfile, destfile in vbaprojectfiles:
                if os.path.exists(srcfile) and os.stat(srcfile).st_size != 0:
                    shutil.copy(srcfile, destfile)
                    os.utime(destfile, (atime, mtime))
            self._zip(self.content_filename, tf_tmpdir)
            syslog.syslog("%s(%s): VBA project upgraded from template - ticket %s (%s)" %
                          (self.trac_env_name, self.authname, self.id, self.ticket_type))


class MSOEFRForm(MSOForm):
    """ MS Office EFR form handling """

    # The following fields are tested for change
    # Additional data on the change may be added
    # for each field (dictionary key), in the form:
    # (old value, new value)
    # Otherwise 'None' is specified

    TICKET_FIELDS = {'company': None,
                     'severity': None,
                     'keywords': None,
                     'document': None,
                     'phase': None,
                     'resolution': None,
                     'submitcomment': None}

    def __init__(self, env, ticket_type, tid, ticket_id, skill, tagname, authname):
        super(MSOEFRForm, self).__init__(env, ticket_type, tid, ticket_id, skill, tagname, authname)
        self.efr_forms_dir = env.config.get('artusplugin', '%s_efr_forms_dir' % skill)
        self.repo_path = '%s%s' % (self.repo and '/%s' % self.repo or '', self.efr_forms_dir)
        self.repo_subpath = self.repo_path
        self._set_paths()
        self._set_urls()
        self.vbaprojectfile = 'word/vbaProject.bin'
        self.vbasignaturefile = 'word/vbaProjectSignature.bin'
        self.vbadatafile = 'word/vbaData.xml'

    def update(self, ticket, ticket_process_data, mode_list, checked, old_values=None):
        """ Update the MSO EFR form(s) data """
        log_level = self.env.config.get('logging', 'log_level')
        if log_level == 'INFO':
            syslog.syslog("%s: Automatic form update !" % ticket_process_data['trac_env_name'])

        trac_data = TracData.get_data(self.env, self.ticket_type)(
            self.env, ticket, ticket_process_data)

        self.form_data.trac_data.data['CreationDateTime'] = trac_data.template_creation_dt
        self.form_data.trac_data.data['ModificationDateTime'] = trac_data.template_modification_dt
        self.form_data.trac_data.data['TemplateRef'] = trac_data.template_ref
        self.form_data.trac_data.data['TracLogin'] = trac_data.authname
        self.form_data.trac_data.data['WFStatus'] = trac_data.status
        self.form_data.trac_data.data['Program'] = trac_data.program
        self.form_data.trac_data.data['Date'] = trac_data.date
        self.form_data.trac_data.data['Company'] = trac_data.company
        self.form_data.trac_data.data['Severity'] = trac_data.severity
        self.form_data.trac_data.data['Summary'] = trac_data.summary
        self.form_data.trac_data.data['Keywords'] = trac_data.keywords
        self.form_data.trac_data.data['Baseline'] = trac_data.baseline
        self.form_data.trac_data.data['Phase'] = trac_data.phase
        self.form_data.trac_data.data['Attachment'] = trac_data.attachment
        self.form_data.trac_data.data['Close_Fixed'] = trac_data.close_fixed
        if trac_data.justification is not None:
            self.form_data.trac_data.data['Justification'] = trac_data.justification
        self.form_data.trac_data.data['Close_Change_Requested'] = trac_data.close_change_requested
        if trac_data.changerequestid is not None:
            self.form_data.trac_data.data['ChangeRequestId'] = trac_data.changerequestid
        self.form_data.trac_data.data['Close_Rejected'] = trac_data.close_rejected
        if trac_data.comment is not None:
            self.form_data.trac_data.data['Comment'] = trac_data.comment
        if trac_data.closure_decision_maker is not None:
            self.form_data.trac_data.data['Closure_Decision_Maker'] = trac_data.closure_decision_maker

        self.form_data.write()

        tmpdir = tempfile.mkdtemp()
        self._unzip(self.content_filename, tmpdir)
        shutil.copy(self.form_filename, self.get_customxml_filepath(tmpdir))
        self._zip(self.content_filename, tmpdir)
        shutil.rmtree(tmpdir)

        # Upgrade VBA, databinding and update modification datetime
        tf = ticket_process_data['ticket_form']
        tft = ticket_process_data['ticket_form_template']
        tf.upgrade(tft)

    def prepare_pdf(self, tkt_fn, up_id):
        # As LibreOffice makes no use of customXML,
        # content controls have to be updated
        # with values from custom XML as
        # those may have been updated by TRAC.
        # A read/write cycle under Word does achieve
        # the same result BUT that cycle may not
        # happen in the case of a TRAC post-treatment
        unix_cmd_list = ['/srv/trac/common/PrepareDocument.sh "%s" "%s"' % (
            tkt_fn, up_id)]
        retcode, lines = util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())
        if retcode != 0:
            raise TracError(_(' '.join(lines)))


class MSOECRForm(MSOForm):
    """ MS Office ECR form handling """

    # The following fields are tested for change
    # Additional data on the change may be added
    # for each field (dictionary key), in the form:
    # (old value, new value)
    # Otherwise 'None' is specified

    TICKET_FIELDS = {'ecrtype': None,
                     'blocking': None,
                     'company': None,
                     'keywords': None,
                     'document': None,
                     'milestone': None,
                     'blockedby': None,
                     'resolution': None,
                     'submitcomment': None,
                     'status': ('04-analysed', '05-assigned_for_implementation')}

    def __init__(self, env, ticket_type, tid, ticket_id, skill, tagname, authname):
        super(MSOECRForm, self).__init__(env, ticket_type, tid, ticket_id, skill, tagname, authname)
        self.ecr_forms_dir = env.config.get('artusplugin', '%s_ecr_forms_dir' % skill)
        self.repo_path = '%s%s' % (self.repo and '/%s' % self.repo or '', self.ecr_forms_dir)
        self.repo_subpath = self.repo_path
        self._set_paths()
        self._set_urls()
        self.vbaprojectfile = 'word/vbaProject.bin'
        self.vbasignaturefile = 'word/vbaProjectSignature.bin'
        self.vbadatafile = 'word/vbaData.xml'

    def update(self, ticket, ticket_process_data, mode_list, checked, old_values=None):
        """ Update the MSO ECR form(s) data """
        log_level = self.env.config.get('logging', 'log_level')
        if log_level == 'INFO':
            syslog.syslog("%s: Automatic form update !" % ticket_process_data['trac_env_name'])

        trac_data = TracData.get_data(self.env, self.ticket_type)(
            self.env, ticket, ticket_process_data)

        self.form_data.trac_data.data['CreationDateTime'] = trac_data.template_creation_dt
        self.form_data.trac_data.data['ModificationDateTime'] = trac_data.template_modification_dt
        self.form_data.trac_data.data['TemplateRef'] = trac_data.template_ref
        self.form_data.trac_data.data['TracLogin'] = trac_data.authname
        self.form_data.trac_data.data['WFStatus'] = trac_data.status
        self.form_data.trac_data.data['Evolution'] = trac_data.evolution
        self.form_data.trac_data.data['Problem_Report'] = trac_data.problem_report
        self.form_data.trac_data.data['Date'] = trac_data.date
        self.form_data.trac_data.data['Parent'] = trac_data.parent
        self.form_data.trac_data.data['Program'] = trac_data.program
        self.form_data.trac_data.data['Author'] = trac_data.author
        self.form_data.trac_data.data['TicketId'] = trac_data.ticketid
        self.form_data.trac_data.data['Title'] = trac_data.title
        self.form_data.trac_data.data['Document'] = trac_data.document
        self.form_data.trac_data.data['Applicable'] = trac_data.applicable
        self.form_data.trac_data.data['Attachments'] = trac_data.attachments
        self.form_data.trac_data.data['Child'] = trac_data.child
        # Search ticket history for 04-analysed to 05-assigned_for_implementation transition
        ticket_module = TicketModule(self.env)
        req = util.get_req()
        changes = [change for change in
                   ticket_module.rendered_changelog_entries(req, ticket)]
        for change in reversed(changes):
            if 'status' in change['fields']:
                old_status = change['fields']['status']['old']
                new_status = change['fields']['status']['new']
                if new_status == self.TICKET_FIELDS['status'][0]:
                    self.form_data.trac_data.data['Implementation_Decision'] = 'false'
                    self.form_data.trac_data.data['Implementation_Decision_Maker'] = ''
                    break
                if (new_status == self.TICKET_FIELDS['status'][1] and
                    old_status == self.TICKET_FIELDS['status'][0]):
                    self.form_data.trac_data.data['Implementation_Decision'] = 'true'
                    self.form_data.trac_data.data['Implementation_Decision_Maker'] = change['author']
                    break
        else:
            self.form_data.trac_data.data['Implementation_Decision'] = 'false'
            self.form_data.trac_data.data['Implementation_Decision_Maker'] = ''
        self.form_data.trac_data.data['Close_Fixed'] = trac_data.close_fixed
        if trac_data.cm_revision is not None:
            self.form_data.trac_data.data['CM_Revision'] = trac_data.cm_revision
        self.form_data.trac_data.data['Close_Rejected'] = trac_data.close_rejected
        if trac_data.comment is not None:
            self.form_data.trac_data.data['Comment'] = trac_data.comment
        if trac_data.closure_decision_maker is not None:
            self.form_data.trac_data.data['Closure_Decision_Maker'] = trac_data.closure_decision_maker

        self.form_data.write()

        tmpdir = tempfile.mkdtemp()
        self._unzip(self.content_filename, tmpdir)
        shutil.copy(self.form_filename, self.get_customxml_filepath(tmpdir))
        self._zip(self.content_filename, tmpdir)
        shutil.rmtree(tmpdir)

        # Upgrade VBA, databinding and update modification datetime
        tf = ticket_process_data['ticket_form']
        tft = ticket_process_data['ticket_form_template']
        tf.upgrade(tft)

    def prepare_pdf(self, tkt_fn, up_id):
        # As LibreOffice makes no use of customXML,
        # content controls have to be updated
        # with values from custom XML as
        # those may have been updated by TRAC.
        # A read/write cycle under Word does achieve
        # the same result BUT that cycle may not
        # happen in the case of a TRAC post-treatment
        unix_cmd_list = ['/srv/trac/common/PrepareDocument.sh "%s" "%s"' % (
            tkt_fn, up_id)]
        retcode, lines = util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())
        if retcode != 0:
            raise TracError(_(' '.join(lines)))


class MSOPRFForm(MSOForm):
    """ MS Office PRF form handling """

    # Modified TRAC data will not be inserted into the PRF Form
    # when the PRF is submitted because technically we don't know
    # how to do this on the server (no automation yet)
    # The import will happen next time through an Edit session.
    # So there is no point in checking modifications on Trac data
    # as it will not lead to a commit anyway
    TICKET_FIELDS = {}

    def __init__(self, env, ticket_type, tid, ticket_id, skill, tagname, authname):
        super(MSOPRFForm, self).__init__(env, ticket_type, tid, ticket_id, skill, tagname, authname)
        self.prf_forms_dir = env.config.get('artusplugin', '%s_prf_forms_dir' % skill if skill else 'prf_forms_dir')
        self.repo_path = '%s%s' % (self.repo and '/%s' % self.repo or '', self.prf_forms_dir)
        self.repo_subpath = '%s/%s' % (self.repo_path, tagname)
        self._set_paths()
        self._set_urls()
        self.sheet_names = ['Cover page', 'Remarks']
        self.vbaprojectfile = 'xl/vbaProject.bin'
        self.vbasignaturefile = 'xl/vbaProjectSignature.bin'

    def _hide_sheets(self, tmpdir):
        """ Hide all check-lists except that of the reviewed document """
        # Show/Hide tabs as required
        chklstname = 'CHKLST_%s_%s' % (
            self.skill,
            NamingRule.get_shortname(self.env,
                                     self.tagname,
                                     self.program_name))
        self.sheet_names.append(chklstname)
        workbook_name = '%s/xl/workbook.xml' % tmpdir
        dom = parse(workbook_name)
        sheets = dom.getElementsByTagName('sheet')
        for sheet in sheets:
            sheetname = sheet.getAttribute("name")
            if sheetname in self.sheet_names:
                if sheet.hasAttribute("state"):
                    sheet.removeAttribute("state")
            else:
                sheet.setAttribute("state", "hidden")
        # Set active tab: Remarks sheet
        workbookViews = dom.getElementsByTagName('workbookView')
        for workbookView in workbookViews:
            workbookView.setAttribute("activeTab", "1")
        # Save
        f = open(workbook_name, 'w')
        xml = []
        xml.append(u'<?xml version="1.0" encoding="utf-8" standalone="yes"?>')
        for child in dom.childNodes:
            xml.append(child.toxml())
        f.writelines(xml)
        f.close

    def upgrade(self, tft):
        template_path = tft.source_name
        if self.get_vba_hash() != tft.get_vba_hash(template_path):
            self.upgrade_vba_legacy(tft, template_path)

    def update(self, ticket, ticket_process_data, mode_list, checked, old_values=None):
        """ Update the MSO PRF form(s) data """
        log_level = self.env.config.get('logging', 'log_level')
        if log_level == 'INFO':
            syslog.syslog("%s: Automatic form update !" % ticket_process_data['trac_env_name'])

        trac_data = TracData.get_data(
            self.env, self.ticket_type)(
                self.env, ticket, ticket_process_data, checked)

        self.form_data.ticket_data.data['WFStatus'] = trac_data.status
        self.form_data.ticket_data.data['ForceEdit'] = trac_data.force_edit
        self.form_data.header_doc_data.data['Program'] = trac_data.program
        self.form_data.header_doc_data.data['Reference'] = trac_data.doc_reference
        self.form_data.header_doc_data.data['Version'] = trac_data.doc_version
        self.form_data.header_doc_data.data['Status'] = trac_data.doc_status
        self.form_data.header_doc_data.data['CMRevision'] = trac_data.trunkrev

        self.form_data.header_prf_data.data['Reference'] = trac_data.rf_reference
        self.form_data.header_prf_data.data['Milestone'] = trac_data.rf_milestone
        self.form_data.header_prf_data.data['Reader'] = trac_data.rf_reader
        self.form_data.header_prf_data.data['Date'] = trac_data.rf_date

        self.form_data.write()

        tmpdir = tempfile.mkdtemp()
        self._unzip(self.content_filename, tmpdir)
        self._hide_sheets(tmpdir)
        self._zip(self.content_filename, tmpdir)
        shutil.rmtree(tmpdir)

        # Upgrade VBA
        tf = ticket_process_data['ticket_form']
        tft = ticket_process_data['ticket_form_template']
        tf.upgrade(tft)

    def prepare_pdf(self, tkt_fn, up_id):
        # Updates form before PDF conversion
        return


class MSOMOMForm(MSOForm):
    """ MS Office MOM form handling """

    # No fields are tested for change
    TICKET_FIELDS = {}

    def __init__(self, env, ticket_type, tid, ticket_id, skill, tagname, authname):
        super(MSOMOMForm, self).__init__(env, ticket_type, tid, ticket_id, skill, tagname, authname)
        self.scheme = env.config.get('artusplugin', 'scheme')

    # Target tables for TY-4.2-09_E3 (old) and TY-4.2-09A/T_E1 (new)
    TARGET_TABLES = ('Tableau6', 'Tableau4')

    # Row structure
    ROW_CELLS = ['A3', 'B3', 'C3', 'D3']

    # Target cells
    TARGET_CELLS = {"A3": 'Text'}

    def add_mom_form_data(self, doc, data):
        """ Add the data to the MoM form """

        for table in doc.getElementsByType(Table):
            table_name = table.getAttribute("name")
            if table_name in self.TARGET_TABLES:
                # First table line (header being excluded)
                template_row = Ooo.get_table_rows(table)[0]
                for row_data in data:
                    Ooo.add_row_to_table(table,
                                         template_row,
                                         self.ROW_CELLS,
                                         self.TARGET_CELLS,
                                         row_data)
                break

    def append_row_data(self, table_data, row_data):
        """
        table_data (OUT): list (OOo document lines)
                          of list (line textual elements)
                          of dictionnaries (key: 'Text')
                          of dictionnaries (keys: 'text','link')
        row_data (IN): list of tuples (text,link)
        """
        data = []
        for elt_row in row_data:
            elt_data = {}
            elt_data['Text'] = {}
            elt_data['Text']['text'], elt_data['Text']['link'] = elt_row
            data.append(elt_data)
        table_data.append(data)

    def append_rows(self, data, requests, db):
        cursor = db.cursor()

        for request_type, title, sql_stmt, param, child_requests in requests:
            # Title
            self.append_row_data(data, [(title, None)])

            # Data
            if request_type != 'section_title':
                # Result of parent statement used as a parameter
                # as many times as necessary
                if param:
                    params = (param,) * sql_stmt.count('%s')
                    sql_stmt = sql_stmt % params
                cursor.execute(sql_stmt)

                # Results are tuples (item, url)
                if request_type == 'ticket':
                    results = [[(sql_result[1],
                                 '%s://%s/tracs/%s/ticket/%s' %
                                 (self.scheme, self.host,
                                  self.trac_env_name, sql_result[1])),
                                (sql_result[0], '%s://%s/tracs/%s/ticket/%s' %
                                 (self.scheme, self.host,
                                  self.trac_env_name, sql_result[1]))]
                               for sql_result in cursor]
                elif request_type == 'attachment':
                    results = [[(sql_result[1],
                                 '%s://%s/tracs/%s/raw-attachment'
                                 '/ticket/%s/%s' %
                                 (self.scheme, self.host,
                                  self.trac_env_name, sql_result[0],
                                  sql_result[1]))]
                               for sql_result in cursor]
                elif request_type == 'version_tag':
                    results = [[(sql_result[0],
                                 '%s://%s/tracs/%s/admin/tags_mgmt'
                                 '/version_tags/%s' %
                                 (self.scheme, self.host,
                                  self.trac_env_name, sql_result[0]
                                  ))]
                               for sql_result in cursor]
                elif request_type == 'milestone_tag':
                    results = [[(sql_result[0],
                                 '%s://%s/tracs/%s/admin/tags_mgmt'
                                 '/milestone_tags/%s' %
                                 (self.scheme, self.host,
                                  self.trac_env_name, sql_result[0]
                                  ))]
                               for sql_result in cursor]
                else:
                    results = None

                if results:
                    for result in results:
                        self.append_row_data(data, result)
                        if child_requests:
                            rqst = []
                            for (rt, tt, ss, (idx1, idx2), cr) in child_requests:
                                rqst.append((rt, tt, ss, result[idx1][idx2], cr))
                            self.append_rows(data, rqst, db)
                else:
                    self.append_row_data(data, [('None', None)])

            self.append_row_data(data, [('', None)])

    def fill_MoM(self, ticket_id, requests, db):
        # Create a symbolic link as an odt file
        # which it is in fact in order to update
        docxpath = self.content_filename
        odtpath = "%s.odt" % docxpath.rsplit('.', 1)[0]
        if not os.access(odtpath, os.F_OK):
            os.symlink(docxpath, odtpath)

        # Open the empty MoM form
        doc = load(odtpath)

        data = []
        self.append_rows(data, requests, db)

        # Write the data to the CCB MoM form
        self.add_mom_form_data(doc, data)

        # Save the MoM form
        Ooo.doc_save(doc, odtpath)
        syslog.syslog("%s(%s): MoM written to odt (ticket %s)" %
                      (self.trac_env_name, self.authname, ticket_id))

        # Convert ticket form to Word - inplace conversion
        unix_cmd_list = ['%s/program/python '
                         '/srv/trac/common/DocumentConverter.py '
                         '"%s" "%s" %s' %
                         (self.env.config.get('artusplugin',
                          'LOo_install_dir'),
                          odtpath,
                          docxpath,
                          self.env.config.get('artusplugin',
                          'LOo_port'))]

        # Effective application of the list of commands -
        retcode = util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())[0]

        if retcode == 0:
            syslog.syslog("%s(%s): MoM converted to docx (ticket %s)" %
                          (self.trac_env_name, self.authname, ticket_id))

    def upgrade(self, tft):
        """ No VBA code """

        return

    def update(self, ticket, ticket_process_data, mode_list, checked, old_values=None):
        """ Update the MSO MOM form(s) data """

        return

    def setup(self, skills, milestone_tag):
        """Prepare an empty MoM form."""

        requests = []
        db = self.env.get_db_cnx()

        self.fill_MoM(self.id, requests, db)


class MSOAuditForm(MSOMOMForm):
    """ MS Office Audit form handling """

    def __init__(self, env, ticket_type, tid, ticket_id, skill, tagname, authname):
        super(MSOAuditForm, self).__init__(env, ticket_type, tid, ticket_id, skill, tagname, authname)
        self.mom_audit_forms_dir = env.config.get('artusplugin', '%s_mom_audit_forms_dir' % skill)
        self.repo_path = '%s%s' % (self.repo and '/%s' % self.repo or '', self.mom_audit_forms_dir)
        self.repo_subpath = self.repo_path
        self._set_paths()
        self._set_urls()


class MSOCCBForm(MSOMOMForm):
    """ MS Office CCB form handling """

    def __init__(self, env, ticket_type, tid, ticket_id, skill, tagname, authname):
        super(MSOCCBForm, self).__init__(env, ticket_type, tid, ticket_id, skill, tagname, authname)
        self.mom_ccb_forms_dir = env.config.get('artusplugin', '%s_mom_ccb_forms_dir' % skill)
        self.repo_path = '%s%s' % (self.repo and '/%s' % self.repo or '', self.mom_ccb_forms_dir)
        self.repo_subpath = self.repo_path
        self._set_paths()
        self._set_urls()

    def _baseline_structure(self, tag):
        stmt1 = ("SELECT name FROM tag "
                 "WHERE name = '%s' " % tag)
        stmt2 = ("SELECT baseline_item.name FROM baseline_item "
                 "WHERE baseline_item.baselined_tag = '%s' "
                 "AND baseline_item.name IN (SELECT tag.name FROM tag "
                 "WHERE tag.baselined = 1) ORDER BY baseline_item.name")
        stmt3 = ("SELECT baseline_item.name FROM baseline_item "
                 "WHERE baseline_item.baselined_tag = '%s' AND "
                 "baseline_item.name IN (SELECT tag.name FROM tag "
                 "WHERE tag.baselined = 1) ORDER BY baseline_item.name")
        return ('milestone_tag',
                'The following milestone is prepared:',
                stmt1,
                None, [('version_tag',
                        'This milestone includes the following '
                        'level 1 baseline(s):',
                        stmt2,
                        (0, 0), [('version_tag',
                                  'The preceding level 1 baseline '
                                  'includes the following level 2 '
                                  'baseline(s):',
                                  stmt3,
                                  (0, 0), None)])])

    def _other_MOMs(self, tid, sk, sql):
        stmt1 = ("SELECT summary, id FROM ticket t, ticket_custom tc, "
                 "ticket_custom tc1, ticket_custom tc2 "
                 "WHERE t.id!=%s AND t.id=tc1.ticket AND t.id=tc2.ticket "
                 "AND t.id=tc.ticket AND t.type='MOM' AND t.status!='closed' "
                 "AND tc1.name='skill' AND tc1.value='%s' "
                 "AND tc2.name='momtype' AND tc2.value = 'CCB' "
                 "AND tc.name='milestonetag' AND %s "
                 "ORDER BY t.id")
        stmt1 %= (tid, sk, sql['with milestone tag'])
        stmt2 = ("SELECT summary, id FROM ticket t, ticket_custom tc "
                 "WHERE t.id=tc.ticket AND t.type = 'AI' "
                 "AND tc.name = 'parent' "
                 "AND SUBSTR(tc.value,2,LENGTH(tc.value))=CAST(%s AS TEXT) "
                 "AND status!='closed'")
        return ('ticket',
                'List of other MOM (%s Configuration Management) '
                'still open' % sk,
                stmt1,
                None, [('ticket',
                        'Associated AI(s) still open',
                        stmt2,
                        (0, 0), None)])

    def _documents_to_release(self, sk, sql):
        if sk in self.env.config.get('artusplugin', 'DOC_skills', '').split('|'):
            # For this skill tags can only be set through DOC tickets
            stmt1 = ("SELECT t.summary, t.id, tc2.value AS tagname FROM ticket t, ticket_custom tc1, ticket_custom tc2 "
                     "WHERE t.id=tc1.ticket AND t.id=tc2.ticket "
                     "AND t.type='DOC' AND t.status='06-assigned_for_release' "
                     "AND tc1.name='skill' AND tc1.value='%s' "
                     "AND tc2.name='document' AND %s "
                     "ORDER BY t.id")
            stmt1 %= (sk, sql['in milestone tag'])
            stmt2 = ("SELECT tc.value FROM ticket t, ticket_custom tc "
                     "WHERE t.id=tc.ticket "
                     "AND t.id=%s "
                     "AND tc.name='document'")
            stmt3 = ("SELECT t.summary, t.id FROM ticket t "
                     "WHERE t.summary LIKE 'PRF\_%s\_%%' ESCAPE '\\' "
                     "ORDER BY t.id")
            stmt4 = ("SELECT a.id, a.filename FROM attachment a "
                     "WHERE a.id = %s AND a.type = 'ticket' "
                     "AND a.filename LIKE 'CHKLST\_%%' ESCAPE '\\'")
            return ('ticket',
                    'List of DOC (%s) in status: 06-assigned_for_release '
                    'and associated document tag and PRF/CHKLST on that tag' % sk,
                    stmt1,
                    None, [('version_tag',
                            'Associated document tag',
                            stmt2,
                            (0, 0), [('ticket',
                                      'Child PRF(s)',
                                      stmt3,
                                      (0, 0), [('attachment',
                                                'Attached checklist(s)',
                                                stmt4,
                                                (0, 0), None)])])])
        else:
            # This skill is NOT associated with DOC tickets
            stmt1 = ("SELECT name AS tagname FROM tag WHERE %s AND name LIKE '%s_%s%%' "
                     "AND status='Proposed' "
                     "AND tag_url IS NOT NULL AND tagged_item NOT IN (%s) "
                     "AND EXISTS (SELECT id FROM ticket WHERE ticket.summary "
                     "LIKE '%%RF\_'||name||'\_%%' ESCAPE '\\') AND NOT EXISTS "
                     "(SELECT id FROM ticket WHERE ticket.summary LIKE "
                     "'%%RF\_'||tagged_item||'.%%' ESCAPE '\\' "
                     "AND ticket.status <> 'closed') ORDER BY name")
            stmt1 %= (sql['in milestone tag'], self.program_name, sk,
                      sql['released versions'] % ("tag.tagged_item", "0"))
            stmt2 = ("SELECT ticket.summary, ticket.id FROM ticket "
                     "WHERE ticket.summary LIKE '%%RF\_%s\_%%' ESCAPE '\\' "
                     "ORDER BY ticket.id")
            stmt3 = ("SELECT attachment.id, attachment.filename FROM attachment "
                     "WHERE attachment.id = %s AND attachment.type = 'ticket' "
                     "AND attachment.filename LIKE 'CHKLST\_%%' ESCAPE '\\'")
            return ('version_tag',
                    'List of documents (%s) in status: Proposed '
                    'and associated (P)RF/CHKLST (for a document version tag '
                    'to be included in the list, there must not exist '
                    'an associated Released tag and there must exist '
                    'a closed P(RF) on its Proposed status '
                    'and there must not exist an open P(RF) '
                    'on another status)' % sk,
                    stmt1,
                    None, [('ticket',
                            'Associated (P)RF(s)',
                            stmt2,
                            (0, 0), [('attachment',
                                      'Attached checklist(s)',
                                      stmt3,
                                      (0, 0), None)])])

    def _components_to_release(self, sk, sql):
        stmt1 = ("SELECT name AS tagname FROM tag WHERE %s AND name LIKE '%s_%s%%' "
                 "AND status='Candidate' "
                 "AND tag_url IS NOT NULL AND tagged_item NOT IN (%s) "
                 "ORDER BY name")
        stmt1 %= (sql['in milestone tag'], self.program_name, sk,
                  sql['released versions'] % ("tag.tagged_item",
                  "ltrim(substr(tag.tag_url,-6),'?rev=')"))
        stmt2 = ("SELECT ticket.summary, ticket.id FROM ticket "
                 "WHERE ticket.summary LIKE '%%RF\_%s\_%%' ESCAPE '\\' "
                 "ORDER BY ticket.id")
        stmt3 = ("SELECT attachment.id, attachment.filename FROM attachment "
                 "WHERE attachment.id = %s AND attachment.type = 'ticket' "
                 "AND attachment.filename LIKE 'CHKLST\_%%' ESCAPE '\\'")
        return ('version_tag',
                'List of components (%s) in status: Candidate '
                'and associated (P)RF/CHKLST (for a component version tag '
                'to be included in the list, there must not exist '
                'an associated subsequent Released tag)' % sk,
                stmt1,
                None, [('ticket',
                        'Associated (P)RF(s)',
                        stmt2,
                        (0, 0), [('attachment',
                                  'Attached checklist(s)',
                                  stmt3,
                                  (0, 0), None)])])

    def _efr_request(self, st, sql):
        return('ticket',
               'List of EFR in status: %s' % st,
               "SELECT summary, id FROM ticket t "
               "WHERE %s AND t.type='EFR' "
               "AND t.status='%s' "
               "ORDER BY t.id" % (sql['with milestone'], st),
               None, None)

    def _ecr_evol_request(self, st, sk, sql):
        """ Prepare a request for ECR of type Evolution """
        return ('ticket',
                'List of ECR (%s) of type Evolution in status: '
                '%s' % (sk, st),
                "SELECT summary, id FROM ticket t, ticket_custom tc1, "
                "ticket_custom tc2 "
                "WHERE %s AND t.id=tc1.ticket AND t.id=tc2.ticket "
                "AND t.type='ECR' AND t.status='%s' AND tc1.name='skill' "
                "AND tc1.value='%s' AND tc2.name='ecrtype' "
                "AND tc2.value='Evolution' "
                "ORDER BY t.id" % (sql['with milestone'], st, sk),
                None, None)

    def _ecr_pr_request(self, st, sk, sql):
        """ Prepare a request for ECR of type Problem Report """
        return ('ticket',
                'List of ECR (%s) of type Problem Report in status: '
                '%s' % (sk, st),
                "SELECT summary, id FROM ticket t, ticket_custom tc1, "
                "ticket_custom tc2 "
                "WHERE %s AND t.id=tc1.ticket AND t.id=tc2.ticket "
                "AND t.type='ECR' AND t.status='%s' AND tc1.name='skill' "
                "AND tc1.value='%s' AND tc2.name='ecrtype' "
                "AND tc2.value='Problem Report' "
                "ORDER BY t.id" % (sql['with milestone'], st, sk),
                None, [('ticket',
                        'Associated EFR(s) in status: %s' % st,
                        "SELECT summary, id FROM ticket t, "
                        "ticket_custom tc "
                        "WHERE instr(' '||replace(tc.value, ',', ' ')||' ', ' '||CAST(%%s AS TEXT)||' ') "
                        "AND t.id=tc.ticket AND t.type='EFR' "
                        "AND t.status='%s' AND tc.name='blockedby' "
                        "ORDER BY t.id" % st,
                        (0, 0), None)])

    def _section_title(self, req, title):
        # Section title
        req.append(('section_title',
                    title, None, None, None))

    def _header(self, req, tag):
        # Milestone/Baselines structure
        req.append(self._baseline_structure(tag))

    def _body(self, req, tid, sk, sql):
        # Other MOM(s) that should be closed
        req.append(self._other_MOMs(tid, sk, sql))

        # documents that might be released
        req.append(self._documents_to_release(sk, sql))

        # components that might be released
        req.append(self._components_to_release(sk, sql))

        # EFR that might be implemented or closed
        if sql['EFR processed'] is False:
            req.append(self._efr_request('07-assigned_for_closure_actions',
                                         sql))
            sql['EFR processed'] = True

        # ECR of type Evolution that might be closed
        req.append(self._ecr_evol_request('07-assigned_for_closure_actions',
                                          sk,
                                          sql))

        # ECR of type Problem Report and associated EFR(s) that might be closed
        req.append(self._ecr_pr_request('07-assigned_for_closure_actions',
                                        sk,
                                        sql))

        # validated analysed ECR of type Evolution
        req.append(self._ecr_evol_request('05-assigned_for_implementation',
                                          sk,
                                          sql))

        # validated analysed ECR of type Problem Report and associated EFR(s)
        req.append(self._ecr_pr_request('05-assigned_for_implementation',
                                        sk,
                                        sql))

    def setup(self, skills, milestone_tag):
        """Prepare a MoM form pre-filled with items
           to be considered by the CCB."""

        # Retrieve the data from TRAC database
        db = self.env.get_db_cnx()

        sql = {}
        sql['released versions'] = (
            "SELECT tag1.tagged_item FROM tag AS tag1 "
            "WHERE tag1.tagged_item=%s AND tag1.status='Released' "
            "AND tag1.tag_url IS NOT NULL "
            "AND ltrim(substr(tag1.tag_url,-6),'?rev=') > %s")

        # request_type, title, sql_stmt, sql_stmt_param_idx, child_requests
        requests = []

        if milestone_tag:
            self._section_title(requests,
                                '======================== '
                                'Milestone/Baselines structure '
                                '========================')
            self._header(requests, milestone_tag)

        if milestone_tag:
            stmt1 = ("SELECT name FROM baseline_item "
                     "WHERE baselined_tag='%s'" % milestone_tag)
            stmt2 = ("SELECT name FROM baseline_item "
                     "WHERE baselined_tag IN (%s)" % stmt1)
            stmt3 = ("SELECT name FROM baseline_item "
                     "WHERE baselined_tag IN (%s)" % stmt2)
            stmt = stmt1 + " UNION " + stmt2 + " UNION " + stmt3

            sql['in milestone tag'] = "tagname IN (%s)" % stmt

            sql['with milestone tag'] = "tc.value = '%s'" % milestone_tag

            cursor = db.cursor()
            cursor.execute("SELECT DISTINCT tagged_item FROM tag "
                           "WHERE name='%s'" % milestone_tag)
            milestone, = cursor.fetchone()

            sql['with milestone'] = "milestone = '%s'" % milestone

        else:
            sql['in milestone tag'] = "1 = 1"
            sql['with milestone tag'] = "1 = 1"
            sql['with milestone'] = "1 = 1"

        if milestone_tag:
            self._section_title(requests,
                                '======================== '
                                'Inside the scope of this CCB '
                                '========================')
        sql['EFR processed'] = False
        for skill in skills:
            self._body(requests, self.id, skill, sql)

        if milestone_tag:
            sql['in milestone tag'] = "tagname NOT IN (%s)" % stmt
            sql['with milestone tag'] = "coalesce(tc.value,'') != '%s'" % milestone_tag
            sql['with milestone'] = "coalesce(milestone,'') != '%s'" % milestone
            self._section_title(requests,
                                '======================= '
                                'Outside the scope of this CCB '
                                '=======================')
            sql['EFR processed'] = False
            for skill in skills:
                self._body(requests, self.id, skill, sql)

        return self.fill_MoM(self.id, requests, db)


class MSOProgressForm(MSOMOMForm):
    """ MS Office Progress form handling """

    def __init__(self, env, ticket_type, tid, ticket_id, skill, tagname, authname):
        super(MSOProgressForm, self).__init__(env, ticket_type, tid, ticket_id, skill, tagname, authname)
        self.mom_progress_forms_dir = env.config.get('artusplugin', '%s_mom_progress_forms_dir' % skill)
        self.repo_path = '%s%s' % (self.repo and '/%s' % self.repo or '', self.mom_progress_forms_dir)
        self.repo_subpath = self.repo_path
        self._set_paths()
        self._set_urls()


class MSOReviewForm(MSOMOMForm):
    """ MS Office Review form handling """

    def __init__(self, env, ticket_type, tid, ticket_id, skill, tagname, authname):
        super(MSOReviewForm, self).__init__(env, ticket_type, tid, ticket_id, skill, tagname, authname)
        self.mom_review_forms_dir = env.config.get('artusplugin', '%s_mom_review_forms_dir' % skill)
        self.repo_path = '%s%s' % (self.repo and '/%s' % self.repo or '', self.mom_review_forms_dir)
        self.repo_subpath = self.repo_path
        self._set_paths()
        self._set_urls()

    def setup(self, skills, milestone_tag):
        """Prepare a MoM form pre-filled with items to be considered in the Review."""

        # request_type, title, sql_stmt, sql_stmt_param_idx, child_requests

        requests = []
        sql = {}
        sql['docs'] = "SELECT tag.name FROM tag WHERE tag.baselined = 0"
        sql['baselines'] = "SELECT tag.name FROM tag WHERE tag.baselined = 1"
        sql['docs milestone'] = "SELECT bi.name AS name,bi.baselined_tag,bi.subpath FROM baseline_item bi WHERE bi.baselined_tag = '%s' AND bi.name IN (%s)" % (milestone_tag, sql['docs'])
        sql['baselines level 1'] = "SELECT bi.name FROM baseline_item bi WHERE bi.baselined_tag = '%s' AND bi.name IN (%s)" % (milestone_tag, sql['baselines'])
        sql['docs baselines level 1'] = "SELECT bi.name AS name,bi.baselined_tag,bi.subpath FROM baseline_item bi WHERE bi.baselined_tag IN (%s) AND bi.name IN (%s)" % (sql['baselines level 1'], sql['docs'])
        sql['baselines level 2'] = "SELECT bi.name FROM baseline_item bi WHERE bi.baselined_tag IN (%s) AND bi.name IN (%s)" % (sql['baselines level 1'], sql['baselines'])
        sql['docs baselines level 2'] = "SELECT bi.name AS name,bi.baselined_tag,bi.subpath FROM baseline_item bi WHERE bi.baselined_tag IN (%s) AND bi.name IN (%s)" % (sql['baselines level 2'], sql['docs'])
        sql['ticket with blocking child'] = "SELECT tc.ticket FROM ticket_custom tc WHERE tc.name='blockedby' AND tc.value!=''"
        sql['tagged_item'] = "SELECT DISTINCT tagged_item from tag WHERE name = '%s'"
        sql['max status index'] = "SELECT max(status_index) from tag WHERE tagged_item = (%s) AND (status = 'Proposed' OR status = 'Candidate')" % sql['tagged_item']

        # Milestone/Baselines structure
        requests.append(('milestone_tag',
                         'The following milestone is reviewed:',
                         "SELECT name FROM tag WHERE name = '%s' " % milestone_tag,
                         None, [('version_tag',
                                 'This milestone includes the following level 1 baseline(s):',
                                 "SELECT baseline_item.name FROM baseline_item WHERE baseline_item.baselined_tag = '%s' AND baseline_item.name IN (SELECT tag.name FROM tag WHERE tag.baselined = 1) ORDER BY baseline_item.name",
                                 (0, 0), [('version_tag',
                                           'The preceding level 1 baseline includes the following level 2 baseline(s):',
                                           "SELECT baseline_item.name FROM baseline_item WHERE baseline_item.baselined_tag = '%s' AND baseline_item.name IN (SELECT tag.name FROM tag WHERE tag.baselined = 1) ORDER BY baseline_item.name",
                                           (0, 0), None)])]))

        # List of all items reviewed
        requests.append(('version_tag',
                         'List of all items reviewed:',
                         "SELECT DISTINCT bi_alias.name FROM (%s UNION %s UNION %s ORDER BY bi.subpath, bi.baselined_tag, bi.name) AS bi_alias" % (sql['docs milestone'], sql['docs baselines level 1'], sql['docs baselines level 2']),
                         None, None))

        # Milestone, Baseline level 1 & level 2 documents/(P)RF/CHKLST grouped by category - union operator selects only distinct values
        requests.append(('version_tag',
                         'List of all reading forms and check-lists done for each item reviewed:',
                         "SELECT DISTINCT bi_alias.name FROM (%s UNION %s UNION %s ORDER BY bi.subpath, bi.baselined_tag, bi.name) AS bi_alias" % (sql['docs milestone'], sql['docs baselines level 1'], sql['docs baselines level 2']),
                         None, [('version_tag',
                                 'Associated proposed or candidate status',
                                 "SELECT name from tag where tagged_item = (%s) AND (status = 'Proposed' OR status = 'Candidate') AND status_index = (%s)" % (sql['tagged_item'], sql['max status index']),
                                 (0, 0), [('ticket',
                                           'Associated (P)RF(s)',
                                           "SELECT ticket.summary, ticket.id FROM ticket WHERE ticket.summary LIKE '%%RF\_%s\_%%' ESCAPE '\\' ORDER BY ticket.id",
                                           (0, 0), [('attachment',
                                                     'Attached checklist(s)',
                                                     "SELECT attachment.id, attachment.filename FROM attachment WHERE attachment.id = %s AND attachment.type = 'ticket' AND attachment.filename LIKE 'CHKLST\_%%' ESCAPE '\\'",
                                                     (0, 0), None)])]),
                                ('version_tag',
                                 'Associated draft or engineering status',
                                 "SELECT name from tag where tagged_item = (%s) AND (status = 'Draft' OR status = 'Engineering')" % sql['tagged_item'],
                                 (0, 0), [('ticket',
                                           'Associated (P)RF(s)',
                                           "SELECT ticket.summary, ticket.id FROM ticket WHERE ticket.summary LIKE '%%RF\_%s\_%%' ESCAPE '\\' ORDER BY ticket.id",
                                           (0, 0), [('attachment',
                                                     'Attached checklist(s)',
                                                     "SELECT attachment.id, attachment.filename FROM attachment WHERE attachment.id = %s AND attachment.type = 'ticket' AND attachment.filename LIKE 'CHKLST\_%%' ESCAPE '\\'",
                                                     (0, 0), None)])])]))

        # open EFRs

        # Retrieve the data from TRAC database
        db = self.env.get_db_cnx()

        if 'SYS' in self.env.config.get('ticket-custom', 'skill.options'):

            EFR_report = self.env.config.get('artusplugin', 'EFR_report')
            cursor = db.cursor()
            cursor.execute("SELECT query FROM report WHERE id=%s" % EFR_report)
            query, = cursor.fetchone()
            query = query.replace('\r', ' ').replace('\n', ' ')

            for skill in skills:
                requests.append(('ticket',
                                 'List of open EFR with %s ECR:' % skill,
                                 "SELECT DISTINCT ticket_alias.'efr id' AS 'summary', ticket_alias.'ticket' AS id FROM (%s) AS ticket_alias  WHERE ticket_alias.'efr status' != 'closed' ORDER BY id" % query.replace('$SKILL', skill),
                                 None, None))

                requests.append(('ticket',
                                 'List of open EFR with no ECR:',
                                 "SELECT ticket.summary, ticket.id FROM ticket WHERE ticket.type = 'EFR' AND ticket.status != 'closed' AND ticket.id NOT IN (%s) ORDER BY id" % sql['ticket with blocking child'],
                                 None, None))
        else:
            for skill in skills:
                requests.append(('ticket',
                                 'List of open EFR on %s with ECR:' % skill,
                                 "SELECT ticket.summary, ticket.id FROM ticket, ticket_custom WHERE ticket.id = ticket_custom.ticket AND ticket.type = 'EFR' AND ticket.status != 'closed' AND ticket.id IN (%s) AND ticket_custom.name = 'skill' AND ticket_custom.value = '%s' ORDER BY id" % (sql['ticket with blocking child'], skill),
                                 None, None))

                requests.append(('ticket',
                                 'List of open EFR on %s with no ECR:' % skill,
                                 "SELECT ticket.summary, ticket.id FROM ticket, ticket_custom WHERE ticket.id = ticket_custom.ticket AND ticket.type = 'EFR' AND ticket.status != 'closed' AND ticket.id NOT IN (%s) AND ticket_custom.name = 'skill' AND ticket_custom.value = '%s' ORDER BY id" % (sql['ticket with blocking child'], skill),
                                 None, None))

        return self.fill_MoM(self.id, requests, db)


ticketform_subclasses = {'OpenOffice': {'EFR': OOoEFRForm,
                                        'ECR': OOoECRForm,
                                        'RF': OOoRFForm
                                        },
                         'MS Office': {'EFR': MSOEFRForm,
                                       'ECR': MSOECRForm,
                                       'PRF': MSOPRFForm,
                                       'MOM': {'Audit': MSOAuditForm,
                                               'CCB': MSOCCBForm,
                                               'Progress': MSOProgressForm,
                                               'Review': MSOReviewForm
                                               }
                                       }
                         }


class TracData(object):
    """ Data that is extracted from Trac database
        for filling in automatically ticket forms """

    @staticmethod
    def get_data(env, ticket_type):
        return tracdata_subclasses[ticket_type]

    def __init__(self, env):
        self.program = env.project_name


class TracEFRData(TracData):
    """ EFR Trac data """

    def __init__(self, env, ticket, ticket_process_data):
        super(TracEFRData, self).__init__(env)

        self.authname = ticket_process_data['authname']
        self.status = ticket['status']
        self.date = ticket.time_created.strftime("%Y-%m-%d")
        self.company = ticket['company']
        self.severity = ticket['severity']
        self.summary = ticket['summary']
        self.keywords = ticket['keywords']
        self.baseline = ticket['document']
        self.phase = ticket['phase']

        attachment_list = None
        for attachment in Attachment.select(env,
                                            ticket.resource.realm,
                                            ticket.resource.id):
            attachment_name = attachment.filename
            if attachment_list:
                attachment_list += ",\n" + attachment_name
            else:
                attachment_list = attachment_name
        if not attachment_list:
            attachment_list = ""
        self.attachment = attachment_list

        self.close_fixed = 'false'
        self.close_change_requested = 'false'
        self.close_rejected = 'false'
        if ticket['resolution'] == 'fixed':
            self.close_fixed = 'true'
        elif ticket['resolution'] == 'change requested':
            self.close_change_requested = 'true'
        elif ticket['resolution'] == 'rejected':
            self.close_rejected = 'true'

        tf = ticket_process_data['ticket_form']
        self.template_creation_dt = tf.get_creation_datetime()
        self.template_modification_dt = tf.get_modification_datetime()
        self.template_ref = tf.get_reference()
        if (ticket['status'] == '07-assigned_for_closure_actions' or
            (ticket['status'] == 'closed' and ticket['resolution'] == 'fixed')):
            self.justification = tf.get_closure_decision_ground(
                env, ticket, ticket['submitcomment'])
            self.changerequestid = ''
            self.comment = ''
        elif ticket['status'] == 'closed' and ticket['resolution'] == 'change requested':
            self.justification = ''
            self.changerequestid = tf.get_closure_decision_ground(
                env, ticket, ticket['submitcomment'])
            self.comment = ''
        elif ticket['status'] == 'closed' and ticket['resolution'] == 'rejected':
            self.justification = ''
            self.changerequestid = ''
            self.comment = tf.get_closure_decision_ground(
                env, ticket, ticket['submitcomment'])
        else:
            self.justification = None
            self.changerequestid = None
            self.comment = None
        if ticket['status'] == 'closed':
            self.closure_decision_maker = ticket_process_data['authname']
        else:
            self.closure_decision_maker = None


class TracECRData(TracData):
    """ ECR Trac data """

    def __init__(self, env, ticket, ticket_process_data):
        super(TracECRData, self).__init__(env)

        self.authname = ticket_process_data['authname']
        self.status = ticket['status']
        self.evolution = 'false'
        self.problem_report = 'false'
        if ticket['ecrtype'] == 'Evolution':
            self.evolution = 'true'
        if ticket['ecrtype'] == 'Problem Report':
            self.problem_report = 'true'
        self.date = ticket.time_created.strftime("%Y-%m-%d")
        parent_list = None
        parent_field_name = 'blocking'
        for parent_id in ticket[parent_field_name].split(','):
            if parent_id == '':
                continue
            else:
                try:
                    parent_ticket = Ticket(env, int(parent_id))
                    if parent_list:
                        parent_list += ",\n" + parent_ticket['summary']
                    else:
                        parent_list = parent_ticket['summary']
                except Exception:
                    continue
        if not parent_list:
            parent_list = ""
        self.parent = parent_list
        self.author = ticket['company']
        self.ticketid = ticket['summary']
        self.title = ticket['keywords']
        self.document = ticket['document']
        self.applicable = ticket['milestone']
        attachment_list = None
        for attachment in Attachment.select(env, ticket.resource.realm, ticket.resource.id):
            attachment_name = attachment.filename
            if attachment_list:
                attachment_list += ",\n" + attachment_name
            else:
                attachment_list = attachment_name
        if not attachment_list:
            attachment_list = ""
        self.attachments = attachment_list
        self.implementation_decision = None
        child_list = None
        for child_id in ticket['blockedby'].split(','):
            if child_id == '':
                continue
            else:
                try:
                    child_ticket = Ticket(env, int(child_id))
                    if child_list:
                        child_list += ",\n" + child_ticket['summary']
                    else:
                        child_list = child_ticket['summary']
                except Exception:
                    continue
        if not child_list:
            child_list = ""
        self.child = child_list
        self.close_fixed = 'false'
        self.close_rejected = 'false'
        if ticket['resolution'] == 'fixed':
            self.close_fixed = 'true'
        elif ticket['resolution'] == 'rejected':
            self.close_rejected = 'true'

        tf = ticket_process_data['ticket_form']
        self.template_creation_dt = tf.get_creation_datetime()
        self.template_modification_dt = tf.get_modification_datetime()
        self.template_ref = tf.get_reference()
        if ticket['status'] == 'closed' and ticket['resolution'] == 'fixed':
            self.cm_revision = tf.get_closure_decision_ground(
                env, ticket, ticket['submitcomment'])
            self.comment = ''
        elif ticket['status'] == 'closed' and ticket['resolution'] == 'rejected':
            self.cm_revision = ''
            self.comment = tf.get_closure_decision_ground(
                env, ticket, ticket['submitcomment'])
        else:
            self.cm_revision = None
            self.comment = None
        if ticket['status'] == 'closed':
            self.closure_decision_maker = ticket_process_data['authname']
        else:
            self.closure_decision_maker = None


class TracPRFData(TracData):
    """ PRF Trac data """

    def __init__(self, env, ticket, ticket_process_data, checked):
        super(TracPRFData, self).__init__(env)

        if ticket['status'] == 'new':
            self.status = '01-assigned_for_description'
        else:
            self.status = ticket['status']
        if checked:
            self.force_edit = 'True'
        else:
            self.force_edit = 'False'

        # The doc title is set by hand for now

        program_name = ticket_process_data['ticket_form'].program_name
        (self.doc_reference,
         self.doc_version,
         self.doc_status) = NamingRule.split_version_tag(env,
                                                         ticket['document'],
                                                         program_name)

        # Get the document subversion revision
        trunkurl, trunkrev = util.get_trunk_url_rev_from_tag(env, ticket)

        # We want the nearest revision to trunkrev
        trunkrev = util.get_last_path_rev_author(env, trunkurl, trunkrev,
                                                 resync=False)[2]

        self.trunkrev = trunkrev

        self.rf_reference = ticket['summary']
        self.rf_milestone = ticket['milestone']
        self.rf_reader = ticket['summary'].rsplit('_', 1)[-1]
        self.rf_date = ticket.time_created.strftime("%Y-%m-%d")


tracdata_subclasses = {'EFR': TracEFRData,
                       'ECR': TracECRData,
                       'PRF': TracPRFData}


class FormData(object):
    """ Data that is exported from form
        or imported into form """

    @staticmethod
    def get_data(env, ticket_type):
        return formdata_subclasses[ticket_type]

    def __init__(self, env, filename, root_tag, handle_ts):
        self.env = env
        self.filename = filename
        self.root_tag = root_tag
        try:
            self.dom = parse(filename)
        except Exception:
            self.dom = None
        self.schemas_url = self.env.config.get('artusplugin', 'schemas_url')
        self.xml_header = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        self.handle_ts = handle_ts

    def write(self, callback):
        f = codecs.open(self.filename, 'w', 'utf-8')
        f.write(callback(True, self.schemas_url))
        f.close()


class FormEFRData(FormData):
    """ EFR form data """

    def __init__(self, env, filename):
        super(FormEFRData, self).__init__(env, filename, 'EFR', True)
        data_tags = ['TracLogin', 'WFStatus',
                     'Program', 'Date', 'Company', 'Severity', 'Summary',
                     'Keywords',
                     'Baseline', 'Phase',
                     'Attachment',
                     'Close_Fixed', 'Justification',
                     'Close_Change_Requested', 'ChangeRequestId',
                     'Close_Rejected', 'Comment',
                     'Closure_Decision_Maker']
        self.trac_data = util.DataSet(self.env, self.dom, self.root_tag, self.handle_ts, data_tags)

    def write(self):
        super(FormEFRData, self).write(self.trac_data.toxml)


class FormECRData(FormData):
    """ ECR form data """

    def __init__(self, env, filename):
        super(FormECRData, self).__init__(env, filename, 'ECR', True)
        data_tags = ['TracLogin', 'WFStatus',
                     'Evolution', 'Problem_Report', 'Date', 'Parent',
                     'Program', 'Author', 'TicketId',
                     'Title',
                     'Document', 'Applicable',
                     'Attachments',
                     'Implementation_Decision', 'Child', 'Implementation_Decision_Maker',
                     'Close_Fixed', 'CM_Revision',
                     'Close_Rejected', 'Comment',
                     'Closure_Decision_Maker']
        self.trac_data = util.DataSet(self.env, self.dom, self.root_tag, self.handle_ts, data_tags)

    def write(self):
        super(FormECRData, self).write(self.trac_data.toxml)


class FormPRFData(FormData):
    """ PRF form data """

    def __init__(self, env, filename):
        super(FormPRFData, self).__init__(env, filename, 'PRF', False)
        root_tag = 'TicketData'
        data_tags = ['WFStatus', 'ForceEdit']
        self.ticket_data = util.DataSet(self.env, self.dom, root_tag, self.handle_ts, data_tags)
        root_tag = 'DocData'
        data_tags = ['Program', 'Reference', 'Version',
                     'Status', 'CMRevision']
        self.header_doc_data = util.DataSet(self.env, self.dom, root_tag, self.handle_ts, data_tags)
        root_tag = 'PRFData'
        data_tags = ['Reference', 'Milestone', 'Reader', 'Date']
        self.header_prf_data = util.DataSet(self.env, self.dom, root_tag, self.handle_ts, data_tags)

    def _toxml(self, namespace, schemas_url):
        xml_string = self.xml_header
        xml_string += (
            '<PRF xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
            ' xsi:noNamespaceSchemaLocation="%s/PRF/schemaPRF.xsd">')
        xml_string %= schemas_url
        xml_string += self.ticket_data.toxml()
        xml_string += self.header_doc_data.toxml()
        xml_string += self.header_prf_data.toxml()
        xml_string += '</PRF>'
        return xml_string

    def write(self):
        super(FormPRFData, self).write(self._toxml)


class FormMOMData(FormData):
    """ MOM form data """

    def __init__(self, env, filename):
        super(FormMOMData, self).__init__(env, filename, 'MOM', False)
        data_tags = []
        self.trac_data = util.DataSet(self.env, self.dom, self.root_tag, self.handle_ts, data_tags)

    def write(self):
        super(FormMOMData, self).write(self.trac_data.toxml)


formdata_subclasses = {'EFR': FormEFRData,
                       'ECR': FormECRData,
                       'PRF': FormPRFData,
                       'MOM': FormMOMData}


class TicketAttachment(object):
    """ This class is used for handling the ticket attachment """

    def __init__(self, env, ticket_form, tid):
        self.env = env
        self.destpath = u'%s/attachments' % ticket_form.path
        self.destpath_bkup = u'%s/.bkup' % self.destpath
        self.http_url = u'%s/attachments' % ticket_form.http_url
        scheme = self.env.config.get('artusplugin', 'scheme')
        self.webdav_protocol = util.get_prop_values(ticket_form.env, 'attachment_edit.protocol')
        if scheme == 'https':
            self.webdav_protocol += 's'
        self.webdav_url = u'%s/attachments' % ticket_form.webdav_url


class CheckList(TicketAttachment):
    """ This class is used for editing the checklist as a ticket attachment """

    TABLE1_CELLS = [
        ['A1', 'B1', 'C1'],
        ['A2', 'B2', 'C2', 'D2', 'E2'],
        ['A3', 'B3', 'C3', 'D3', 'E3'],
        ['A4', 'B4', 'C4', 'D4', 'E4'],
        ['A5', 'B5', 'C5', 'D5', 'E5'],
        ['A6', 'B6', 'C6', 'D6', 'E6'],
        ['A7', 'B7', 'C7', 'D7', 'E7']]

    TEXTUAL_CELLS = {
        "B2": 'Program',
        "B4": 'DocReference',
        "B5": 'DocVersion',
        "B6": 'DocStatus',
        "B7": 'DocRevision',
        "E2": 'CHKLSTReference',
        "E3": 'Checker',
        "E4": 'CheckListDate'}

    def __init__(self, env, ticket, data, attachment, checker):
        super(CheckList, self).__init__(env, data['ticket_form'],
                                        data['id'])
        self.ticket = ticket
        self.data = data
        self.attachment = attachment
        self.checker = checker
        self.program_name = util.get_program_data(env)['program_name']

    def update(self):
        """ Open, modify and save the Ooo attachment """
        syslog.syslog("Automatic attachment update !")

        # Attachment pathname
        pathname = self.attachment.path

        # Open the attachment
        doc = load(pathname)

        # Write the data to the attachment
        self.set_data(doc, self.get_data())

        # Save the attachment
        Ooo.doc_save(doc, pathname)

    def get_data(self):
        """ Get the data for the attachment header """

        ticket_data = {}

        ticket_data['Program'] = {}
        ticket_data['Program']['type'] = Ooo.STRING_VALUE_TYPE
        if self.env.project_name.endswith(' SW'):
            ticket_data['Program']['text'] = self.env.project_name[:-3]
        else:
            ticket_data['Program']['text'] = self.env.project_name

        # Document title set by hand (ignored)

        # Document reference, version and status
        ticket_data['DocReference'] = {}
        ticket_data['DocVersion'] = {}
        ticket_data['DocStatus'] = {}
        ticket_data['DocReference']['type'] = Ooo.STRING_VALUE_TYPE
        ticket_data['DocVersion']['type'] = Ooo.STRING_VALUE_TYPE
        ticket_data['DocStatus']['type'] = Ooo.STRING_VALUE_TYPE

        # No check for unmanaged skills :
        # edition, revision and status are NOT extracted from document reference and
        # therefore are NOT copied by TRAC because of the unknown formalism
        # only the document reference is copied by TRAC for unmanaged skills
        if not util.skill_is_unmanaged(self.env, self.ticket['document']):
            # Internal document
            split_ref, doc_revision, doc_status = self.ticket['document'].split('.')
            doc_reference, doc_edition = split_ref.rsplit('_', 1)
            ticket_data['DocReference']['text'] = doc_reference
            ticket_data['DocVersion']['text'] = doc_edition + '.' + doc_revision
            ticket_data['DocStatus']['text'] = doc_status
        else:
            # External document
            ticket_data['DocReference']['text'] = self.ticket['document']
            ticket_data['DocVersion']['text'] = ''
            ticket_data['DocStatus']['text'] = ''

        # Document CM Revision
        ticket_data['DocRevision'] = {}
        ticket_data['DocRevision']['type'] = Ooo.STRING_VALUE_TYPE
        trunkurl, trunkrev = util.get_trunk_url_rev_from_tag(self.env, self.ticket)

        # We want the nearest revision to trunkrev
        trunkrev = util.get_last_path_rev_author(self.env, trunkurl, trunkrev, resync=False)[2]

        ticket_data['DocRevision']['text'] = trunkrev

        # Checklist Reference
        ticket_data['CHKLSTReference'] = {}
        ticket_data['CHKLSTReference']['type'] = Ooo.STRING_VALUE_TYPE
        ticket_data['CHKLSTReference']['text'] = self.attachment.filename.rsplit('.', 1)[0]

        # Checker
        ticket_data['Checker'] = {}
        ticket_data['Checker']['type'] = Ooo.STRING_VALUE_TYPE
        ticket_data['Checker']['text'] = self.checker

        ticket_data['CheckListDate'] = {}
        ticket_data['CheckListDate']['type'] = Ooo.DATE_VALUE_TYPE
        ticket_data['CheckListDate']['text'] = self.attachment.date.strftime("%Y-%m-%d")

        return ticket_data

    def set_data(self, doc, ticket_data):
        """ Write the data to the attachment """

        for table in doc.getElementsByType(Table):
            if table.getAttribute("name") == 'Tableau9':
                Ooo.insert_text_into_cells(table, self.TABLE1_CELLS, self.TEXTUAL_CELLS, ticket_data)
                break


class MoM(TicketAttachment):
    """ This class is used for editing the MoM as a ticket attachment """

    # Target tables for TY-4.2-09_E3 (old) and TY-4.2-09A/T_E1 (new)
    TARGET_TABLES = ('Tableau6', 'Tableau4')

    # Row structure
    ROW_CELLS = ['A3', 'B3', 'C3', 'D3']

    # Target cells
    TARGET_CELLS = {"A3": 'Text'}

    def __init__(self, env, host, authname):
        self.env = env
        program_data = util.get_program_data(env)
        self.trac_env_name = program_data['trac_env_name']
        self.program_name = program_data['program_name']
        self.host = host
        self.authname = authname
        self.scheme = self.env.config.get('artusplugin', 'scheme')

    def add_mom_form_data(self, doc, data):
        """ Add the data to the MoM form """

        for table in doc.getElementsByType(Table):
            table_name = table.getAttribute("name")
            if table_name in self.TARGET_TABLES:
                # First table line (header being excluded)
                template_row = Ooo.get_table_rows(table)[0]
                for row_data in data:
                    Ooo.add_row_to_table(table,
                                         template_row,
                                         self.ROW_CELLS,
                                         self.TARGET_CELLS,
                                         row_data)
                break

    def append_row_data(self, table_data, row_data):
        """
        table_data (OUT): list (OOo document lines)
                          of list (line textual elements)
                          of dictionnaries (key: 'Text')
                          of dictionnaries (keys: 'text','link')
        row_data (IN): list of tuples (text,link)
        """
        data = []
        for elt_row in row_data:
            elt_data = {}
            elt_data['Text'] = {}
            elt_data['Text']['text'], elt_data['Text']['link'] = elt_row
            data.append(elt_data)
        table_data.append(data)

    def append_rows(self, data, requests, db):
        cursor = db.cursor()

        for request_type, title, sql_stmt, param, child_requests in requests:
            # Title
            self.append_row_data(data, [(title, None)])

            # Data
            if request_type != 'section_title':
                # Result of parent statement used as a parameter
                # as many times as necessary
                if param:
                    params = (param,) * sql_stmt.count('%s')
                    sql_stmt = sql_stmt % params
                cursor.execute(sql_stmt)

                # Results are tuples (item, url)
                if request_type == 'ticket':
                    results = [[(sql_result[1],
                                 '%s://%s/tracs/%s/ticket/%s' %
                                 (self.scheme, self.host,
                                  self.trac_env_name, sql_result[1])),
                                (sql_result[0], '%s://%s/tracs/%s/ticket/%s' %
                                 (self.scheme, self.host,
                                  self.trac_env_name, sql_result[1]))]
                               for sql_result in cursor]
                elif request_type == 'attachment':
                    results = [[(sql_result[1],
                                 '%s://%s/tracs/%s/raw-attachment'
                                 '/ticket/%s/%s' %
                                 (self.scheme, self.host,
                                  self.trac_env_name, sql_result[0],
                                  sql_result[1]))]
                               for sql_result in cursor]
                elif request_type == 'version_tag':
                    results = [[(sql_result[0],
                                 '%s://%s/tracs/%s/admin/tags_mgmt'
                                 '/version_tags/%s' %
                                 (self.scheme, self.host,
                                  self.trac_env_name, sql_result[0]
                                  ))]
                               for sql_result in cursor]
                elif request_type == 'milestone_tag':
                    results = [[(sql_result[0],
                                 '%s://%s/tracs/%s/admin/tags_mgmt'
                                 '/milestone_tags/%s' %
                                 (self.scheme, self.host,
                                  self.trac_env_name, sql_result[0]
                                  ))]
                               for sql_result in cursor]
                else:
                    results = None

                if results:
                    for result in results:
                        self.append_row_data(data, result)
                        if child_requests:
                            rqst = []
                            for (rt, tt, ss, (idx1, idx2), cr) in child_requests:
                                rqst.append((rt, tt, ss, result[idx1][idx2], cr))
                            self.append_rows(data, rqst, db)
                else:
                    self.append_row_data(data, [('None', None)])

            self.append_row_data(data, [('', None)])

    def fill_MoM(self, ticket_id, requests, db):
        MOM_DIRECTORY = '/var/cache/trac/MoM'
        MOM_FILE = 'MoM.odt'

        # Creates MOM_DIRECTORY if it does not exist
        if not os.access(MOM_DIRECTORY, os.F_OK):
            os.mkdir(MOM_DIRECTORY)

        # The setup will be done under a temporary sub-directory
        # of MOM_DIRECTORY named as follows:
        # <trac_env_name>_<datetime>_<authname>
        base_path = '%s/%s_%s_%s' % (
            MOM_DIRECTORY, self.trac_env_name,
            unicode(datetime.now(localtz).strftime('%x_%X')).
            replace('/', '-').replace(':', '-'), self.authname)
        if not os.access(base_path, os.F_OK):
            os.mkdir(base_path)

        # Retrieve the empty MoM form
        odtpath = '%s/%s' % (base_path, MOM_FILE)
        for template_dir in Chrome(self.env).get_templates_dirs():
            srcpath = '%s/%s' % (template_dir, self.env.config.get(
                'artusplugin', 'MOM_template'))
            if os.path.exists(srcpath):
                shutil.copy(srcpath, odtpath)
                break

        # Open the empty MoM form
        doc = load(odtpath)

        data = []
        self.append_rows(data, requests, db)

        # Write the data to the CCB MoM form
        self.add_mom_form_data(doc, data)

        # Save the MoM form
        Ooo.doc_save(doc, odtpath)
        syslog.syslog("%s(%s): MoM written to odt (ticket %s)" %
                      (self.trac_env_name, self.authname, ticket_id))

        # Convert ticket form to Word
        docxpath = "%s.docx" % odtpath.rsplit('.', 1)[0]
        unix_cmd_list = ['%s/program/python '
                         '/srv/trac/common/DocumentConverter.py '
                         '"%s" "%s" %s' %
                         (self.env.config.get('artusplugin',
                          'LOo_install_dir'),
                          odtpath,
                          docxpath,
                          self.env.config.get('artusplugin',
                          'LOo_port'))]
        # Effective application of the list of commands -
        retcode = util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())[0]

        if retcode == 0:
            syslog.syslog("%s(%s): MoM converted to docx (ticket %s)" %
                          (self.trac_env_name, self.authname, ticket_id))

        return docxpath

    def setup(self, ticket_id, skills, milestone_tag):
        """Prepare an empty MoM form."""

        requests = []
        db = self.env.get_db_cnx()

        return self.fill_MoM(ticket_id, requests, db)


class CCB(MoM):

    def __init__(self, env, host, authname):
        super(CCB, self).__init__(env, host, authname)

    def _baseline_structure(self, tag):
        stmt1 = ("SELECT name FROM tag "
                 "WHERE name = '%s' " % tag)
        stmt2 = ("SELECT baseline_item.name FROM baseline_item "
                 "WHERE baseline_item.baselined_tag = '%s' "
                 "AND baseline_item.name IN (SELECT tag.name FROM tag "
                 "WHERE tag.baselined = 1) ORDER BY baseline_item.name")
        stmt3 = ("SELECT baseline_item.name FROM baseline_item "
                 "WHERE baseline_item.baselined_tag = '%s' AND "
                 "baseline_item.name IN (SELECT tag.name FROM tag "
                 "WHERE tag.baselined = 1) ORDER BY baseline_item.name")
        return ('milestone_tag',
                'The following milestone is prepared:',
                stmt1,
                None, [('version_tag',
                        'This milestone includes the following '
                        'level 1 baseline(s):',
                        stmt2,
                        (0, 0), [('version_tag',
                                  'The preceding level 1 baseline '
                                  'includes the following level 2 '
                                  'baseline(s):',
                                  stmt3,
                                  (0, 0), None)])])

    def _other_MOMs(self, tid, sk, sql):
        stmt1 = ("SELECT summary, id FROM ticket t, ticket_custom tc, "
                 "ticket_custom tc1, ticket_custom tc2 "
                 "WHERE t.id!=%s AND t.id=tc1.ticket AND t.id=tc2.ticket "
                 "AND t.id=tc.ticket AND t.type='MOM' AND t.status!='closed' "
                 "AND tc1.name='skill' AND tc1.value='%s' "
                 "AND tc2.name='momtype' AND tc2.value = 'CCB' "
                 "AND tc.name='milestonetag' AND %s "
                 "ORDER BY t.id")
        stmt1 %= (tid, sk, sql['with milestone tag'])
        stmt2 = ("SELECT summary, id FROM ticket t, ticket_custom tc "
                 "WHERE t.id=tc.ticket AND t.type = 'AI' "
                 "AND tc.name = 'parent' "
                 "AND SUBSTR(tc.value,2,LENGTH(tc.value))=CAST(%s AS TEXT) "
                 "AND status!='closed'")
        return ('ticket',
                'List of other MOM (%s Configuration Management) '
                'still open' % sk,
                stmt1,
                None, [('ticket',
                        'Associated AI(s) still open',
                        stmt2,
                        (0, 0), None)])

    def _documents_to_release(self, sk, sql):
        stmt1 = ("SELECT name AS tagname FROM tag WHERE %s AND name LIKE '%s_%s%%' "
                 "AND status='Proposed' "
                 "AND tag_url IS NOT NULL AND tagged_item NOT IN (%s) "
                 "AND EXISTS (SELECT id FROM ticket WHERE ticket.summary "
                 "LIKE '%%RF\_'||name||'\_%%' ESCAPE '\\') AND NOT EXISTS "
                 "(SELECT id FROM ticket WHERE ticket.summary LIKE "
                 "'%%RF\_'||tagged_item||'.%%' ESCAPE '\\' "
                 "AND ticket.status <> 'closed') ORDER BY name")
        stmt1 %= (sql['in milestone tag'], self.program_name, sk,
                  sql['released versions'] % ("tag.tagged_item", "0"))
        stmt2 = ("SELECT ticket.summary, ticket.id FROM ticket "
                 "WHERE ticket.summary LIKE '%%RF\_%s\_%%' ESCAPE '\\' "
                 "ORDER BY ticket.id")
        stmt3 = ("SELECT attachment.id, attachment.filename FROM attachment "
                 "WHERE attachment.id = %s AND attachment.type = 'ticket' "
                 "AND attachment.filename LIKE 'CHKLST\_%%' ESCAPE '\\'")
        return ('version_tag',
                'List of documents (%s) in status: Proposed '
                'and associated (P)RF/CHKLST (for a document version tag '
                'to be included in the list, there must not exist '
                'an associated Released tag and there must exist '
                'a closed P(RF) on its Proposed status '
                'and there must not exist an open P(RF) '
                'on another status)' % sk,
                stmt1,
                None, [('ticket',
                        'Associated (P)RF(s)',
                        stmt2,
                        (0, 0), [('attachment',
                                  'Attached checklist(s)',
                                  stmt3,
                                  (0, 0), None)])])

    def _components_to_release(self, sk, sql):
        stmt1 = ("SELECT name AS tagname FROM tag WHERE %s AND name LIKE '%s_%s%%' "
                 "AND status='Candidate' "
                 "AND tag_url IS NOT NULL AND tagged_item NOT IN (%s) "
                 "ORDER BY name")
        stmt1 %= (sql['in milestone tag'], self.program_name, sk,
                  sql['released versions'] % ("tag.tagged_item",
                  "ltrim(substr(tag.tag_url,-6),'?rev=')"))
        stmt2 = ("SELECT ticket.summary, ticket.id FROM ticket "
                 "WHERE ticket.summary LIKE '%%RF\_%s\_%%' ESCAPE '\\' "
                 "ORDER BY ticket.id")
        stmt3 = ("SELECT attachment.id, attachment.filename FROM attachment "
                 "WHERE attachment.id = %s AND attachment.type = 'ticket' "
                 "AND attachment.filename LIKE 'CHKLST\_%%' ESCAPE '\\'")
        return ('version_tag',
                'List of components (%s) in status: Candidate '
                'and associated (P)RF/CHKLST (for a component version tag '
                'to be included in the list, there must not exist '
                'an associated subsequent Released tag)' % sk,
                stmt1,
                None, [('ticket',
                        'Associated (P)RF(s)',
                        stmt2,
                        (0, 0), [('attachment',
                                  'Attached checklist(s)',
                                  stmt3,
                                  (0, 0), None)])])

    def _efr_request(self, st, sql):
        return('ticket',
               'List of EFR in status: %s' % st,
               "SELECT summary, id FROM ticket t "
               "WHERE %s AND t.type='EFR' "
               "AND t.status='%s' "
               "ORDER BY t.id" % (sql['with milestone'], st),
               None, None)

    def _ecr_evol_request(self, st, sk, sql):
        """ Prepare a request for ECR of type Evolution """
        return ('ticket',
                'List of ECR (%s) of type Evolution in status: '
                '%s' % (sk, st),
                "SELECT summary, id FROM ticket t, ticket_custom tc1, "
                "ticket_custom tc2 "
                "WHERE %s AND t.id=tc1.ticket AND t.id=tc2.ticket "
                "AND t.type='ECR' AND t.status='%s' AND tc1.name='skill' "
                "AND tc1.value='%s' AND tc2.name='ecrtype' "
                "AND tc2.value='Evolution' "
                "ORDER BY t.id" % (sql['with milestone'], st, sk),
                None, None)

    def _ecr_pr_request(self, st, sk, sql):
        """ Prepare a request for ECR of type Problem Report """
        return ('ticket',
                'List of ECR (%s) of type Problem Report in status: '
                '%s' % (sk, st),
                "SELECT summary, id FROM ticket t, ticket_custom tc1, "
                "ticket_custom tc2 "
                "WHERE %s AND t.id=tc1.ticket AND t.id=tc2.ticket "
                "AND t.type='ECR' AND t.status='%s' AND tc1.name='skill' "
                "AND tc1.value='%s' AND tc2.name='ecrtype' "
                "AND tc2.value='Problem Report' "
                "ORDER BY t.id" % (sql['with milestone'], st, sk),
                None, [('ticket',
                        'Associated EFR(s) in status: %s' % st,
                        "SELECT summary, id FROM ticket t, "
                        "ticket_custom tc "
                        "WHERE instr(' '||replace(tc.value, ',', ' ')||' ', ' '||CAST(%%s AS TEXT)||' ') "
                        "AND t.id=tc.ticket AND t.type='EFR' "
                        "AND t.status='%s' AND tc.name='blockedby' "
                        "ORDER BY t.id" % st,
                        (0, 0), None)])

    def _section_title(self, req, title):
        # Section title
        req.append(('section_title',
                    title, None, None, None))

    def _header(self, req, tag):
        # Milestone/Baselines structure
        req.append(self._baseline_structure(tag))

    def _body(self, req, tid, sk, sql):
        # Other MOM(s) that should be closed
        req.append(self._other_MOMs(tid, sk, sql))

        # documents that might be released
        req.append(self._documents_to_release(sk, sql))

        # components that might be released
        req.append(self._components_to_release(sk, sql))

        # EFR that might be implemented or closed
        if sql['EFR processed'] is False:
            req.append(self._efr_request('07-assigned_for_closure_actions',
                                         sql))
            sql['EFR processed'] = True

        # ECR of type Evolution that might be closed
        req.append(self._ecr_evol_request('07-assigned_for_closure_actions',
                                          sk,
                                          sql))

        # ECR of type Problem Report and associated EFR(s) that might be closed
        req.append(self._ecr_pr_request('07-assigned_for_closure_actions',
                                        sk,
                                        sql))

        # validated analysed ECR of type Evolution
        req.append(self._ecr_evol_request('05-assigned_for_implementation',
                                          sk,
                                          sql))

        # validated analysed ECR of type Problem Report and associated EFR(s)
        req.append(self._ecr_pr_request('05-assigned_for_implementation',
                                        sk,
                                        sql))

    def setup(self, ticket_id, skills, milestone_tag):
        """Prepare a MoM form pre-filled with items
           to be considered by the CCB."""

        # Retrieve the data from TRAC database
        db = self.env.get_db_cnx()

        sql = {}
        sql['released versions'] = (
            "SELECT tag1.tagged_item FROM tag AS tag1 "
            "WHERE tag1.tagged_item=%s AND tag1.status='Released' "
            "AND tag1.tag_url IS NOT NULL "
            "AND ltrim(substr(tag1.tag_url,-6),'?rev=') > %s")

        # request_type, title, sql_stmt, sql_stmt_param_idx, child_requests
        requests = []

        if milestone_tag:
            self._section_title(requests,
                                '======================== '
                                'Milestone/Baselines structure '
                                '========================')
            self._header(requests, milestone_tag)

        if milestone_tag:
            stmt1 = ("SELECT name FROM baseline_item "
                     "WHERE baselined_tag='%s'" % milestone_tag)
            stmt2 = ("SELECT name FROM baseline_item "
                     "WHERE baselined_tag IN (%s)" % stmt1)
            stmt3 = ("SELECT name FROM baseline_item "
                     "WHERE baselined_tag IN (%s)" % stmt2)
            stmt = stmt1 + " UNION " + stmt2 + " UNION " + stmt3

            sql['in milestone tag'] = "tagname IN (%s)" % stmt

            sql['with milestone tag'] = "tc.value = '%s'" % milestone_tag

            cursor = db.cursor()
            cursor.execute("SELECT DISTINCT tagged_item FROM tag "
                           "WHERE name='%s'" % milestone_tag)
            milestone, = cursor.fetchone()

            sql['with milestone'] = "milestone = '%s'" % milestone

        else:
            sql['in milestone tag'] = "1 = 1"
            sql['with milestone tag'] = "1 = 1"
            sql['with milestone'] = "1 = 1"

        if milestone_tag:
            self._section_title(requests,
                                '======================== '
                                'Inside the scope of this CCB '
                                '========================')
        sql['EFR processed'] = False
        for skill in skills:
            self._body(requests, ticket_id, skill, sql)

        if milestone_tag:
            sql['in milestone tag'] = "tagname NOT IN (%s)" % stmt
            sql['with milestone tag'] = "coalesce(tc.value,'') != '%s'" % milestone_tag
            sql['with milestone'] = "coalesce(milestone,'') != '%s'" % milestone
            self._section_title(requests,
                                '======================= '
                                'Outside the scope of this CCB '
                                '=======================')
            sql['EFR processed'] = False
            for skill in skills:
                self._body(requests, ticket_id, skill, sql)

        return self.fill_MoM(ticket_id, requests, db)


class Review(MoM):

    def __init__(self, env, host, authname):
        super(Review, self).__init__(env, host, authname)

    def setup(self, ticket_id, skills, milestone_tag):
        """Prepare a MoM form pre-filled with items to be considered in the Review."""

        # request_type, title, sql_stmt, sql_stmt_param_idx, child_requests

        requests = []
        sql = {}
        sql['docs'] = "SELECT tag.name FROM tag WHERE tag.baselined = 0"
        sql['baselines'] = "SELECT tag.name FROM tag WHERE tag.baselined = 1"
        sql['docs milestone'] = "SELECT bi.name AS name,bi.baselined_tag,bi.subpath FROM baseline_item bi WHERE bi.baselined_tag = '%s' AND bi.name IN (%s)" % (milestone_tag, sql['docs'])
        sql['baselines level 1'] = "SELECT bi.name FROM baseline_item bi WHERE bi.baselined_tag = '%s' AND bi.name IN (%s)" % (milestone_tag, sql['baselines'])
        sql['docs baselines level 1'] = "SELECT bi.name AS name,bi.baselined_tag,bi.subpath FROM baseline_item bi WHERE bi.baselined_tag IN (%s) AND bi.name IN (%s)" % (sql['baselines level 1'], sql['docs'])
        sql['baselines level 2'] = "SELECT bi.name FROM baseline_item bi WHERE bi.baselined_tag IN (%s) AND bi.name IN (%s)" % (sql['baselines level 1'], sql['baselines'])
        sql['docs baselines level 2'] = "SELECT bi.name AS name,bi.baselined_tag,bi.subpath FROM baseline_item bi WHERE bi.baselined_tag IN (%s) AND bi.name IN (%s)" % (sql['baselines level 2'], sql['docs'])
        sql['ticket with blocking child'] = "SELECT tc.ticket FROM ticket_custom tc WHERE tc.name='blockedby' AND tc.value!=''"
        sql['tagged_item'] = "SELECT DISTINCT tagged_item from tag WHERE name = '%s'"
        sql['max status index'] = "SELECT max(status_index) from tag WHERE tagged_item = (%s) AND (status = 'Proposed' OR status = 'Candidate')" % sql['tagged_item']

        # Milestone/Baselines structure
        requests.append(('milestone_tag',
                         'The following milestone is reviewed:',
                         "SELECT name FROM tag WHERE name = '%s' " % milestone_tag,
                         None, [('version_tag',
                                 'This milestone includes the following level 1 baseline(s):',
                                 "SELECT baseline_item.name FROM baseline_item WHERE baseline_item.baselined_tag = '%s' AND baseline_item.name IN (SELECT tag.name FROM tag WHERE tag.baselined = 1) ORDER BY baseline_item.name",
                                 (0, 0), [('version_tag',
                                           'The preceding level 1 baseline includes the following level 2 baseline(s):',
                                           "SELECT baseline_item.name FROM baseline_item WHERE baseline_item.baselined_tag = '%s' AND baseline_item.name IN (SELECT tag.name FROM tag WHERE tag.baselined = 1) ORDER BY baseline_item.name",
                                           (0, 0), None)])]))

        # List of all items reviewed
        requests.append(('version_tag',
                         'List of all items reviewed:',
                         "SELECT DISTINCT bi_alias.name FROM (%s UNION %s UNION %s ORDER BY bi.subpath, bi.baselined_tag, bi.name) AS bi_alias" % (sql['docs milestone'], sql['docs baselines level 1'], sql['docs baselines level 2']),
                         None, None))

        # Milestone, Baseline level 1 & level 2 documents/(P)RF/CHKLST grouped by category - union operator selects only distinct values
        requests.append(('version_tag',
                         'List of all reading forms and check-lists done for each item reviewed:',
                         "SELECT DISTINCT bi_alias.name FROM (%s UNION %s UNION %s ORDER BY bi.subpath, bi.baselined_tag, bi.name) AS bi_alias" % (sql['docs milestone'], sql['docs baselines level 1'], sql['docs baselines level 2']),
                         None, [('version_tag',
                                 'Associated proposed or candidate status',
                                 "SELECT name from tag where tagged_item = (%s) AND (status = 'Proposed' OR status = 'Candidate') AND status_index = (%s)" % (sql['tagged_item'], sql['max status index']),
                                 (0, 0), [('ticket',
                                           'Associated (P)RF(s)',
                                           "SELECT ticket.summary, ticket.id FROM ticket WHERE ticket.summary LIKE '%%RF\_%s\_%%' ESCAPE '\\' ORDER BY ticket.id",
                                           (0, 0), [('attachment',
                                                     'Attached checklist(s)',
                                                     "SELECT attachment.id, attachment.filename FROM attachment WHERE attachment.id = %s AND attachment.type = 'ticket' AND attachment.filename LIKE 'CHKLST\_%%' ESCAPE '\\'",
                                                     (0, 0), None)])]),
                                ('version_tag',
                                 'Associated draft or engineering status',
                                 "SELECT name from tag where tagged_item = (%s) AND (status = 'Draft' OR status = 'Engineering')" % sql['tagged_item'],
                                 (0, 0), [('ticket',
                                           'Associated (P)RF(s)',
                                           "SELECT ticket.summary, ticket.id FROM ticket WHERE ticket.summary LIKE '%%RF\_%s\_%%' ESCAPE '\\' ORDER BY ticket.id",
                                           (0, 0), [('attachment',
                                                     'Attached checklist(s)',
                                                     "SELECT attachment.id, attachment.filename FROM attachment WHERE attachment.id = %s AND attachment.type = 'ticket' AND attachment.filename LIKE 'CHKLST\_%%' ESCAPE '\\'",
                                                     (0, 0), None)])])]))

        # open EFRs

        # Retrieve the data from TRAC database
        db = self.env.get_db_cnx()

        EFR_report = self.env.config.get('artusplugin', 'EFR_report')
        cursor = db.cursor()
        cursor.execute("SELECT query FROM report WHERE id=%s" % EFR_report)
        query, = cursor.fetchone()
        query = query.replace('\r', ' ').replace('\n', ' ')

        for skill in skills:

            requests.append(('ticket',
                             'List of open EFR with %s ECR:' % skill,
                             "SELECT DISTINCT ticket_alias.'efr id' AS 'summary', ticket_alias.'ticket' AS id FROM (%s) AS ticket_alias  WHERE ticket_alias.'efr status' != 'closed' ORDER BY id" % query.replace('$SKILL', skill),
                             None, None))

        requests.append(('ticket',
                         'List of open EFR with no ECR:',
                         "SELECT ticket.summary, ticket.id FROM ticket WHERE ticket.type = 'EFR' AND ticket.status != 'closed' AND ticket.id NOT IN (%s) ORDER BY id" % sql['ticket with blocking child'],
                         None, None))

        return self.fill_MoM(ticket_id, requests, db)
