# -*- coding: utf-8 -*-
#
# Copyright (C) 2016 Artus
# All rights reserved.
#
# Author: Michel Guillot <michel.guillot@meggitt.com>

""" Caching (working copy) of configuration items """

from __builtin__ import unicode

# Genshi
from genshi.builder import tag
from genshi.output import DocType
from genshi.template import TemplateLoader, MarkupTemplate
from genshi.template.loader import TemplateNotFound

# Trac
from trac.attachment import Attachment
from trac.core import TracError
from trac.ticket import Ticket
from trac.ticket.model import Type
from trac.util import get_pkginfo
from trac.util.datefmt import localtz
from trac.util.text import unicode_quote, pretty_size
from trac.versioncontrol.api import NoSuchNode
from trac.web.chrome import Chrome

# Standard lib
import cgi
import codecs
import fnmatch
import os
import re
import hashlib
import shutil
import smtplib
import subprocess
import sys
import syslog
import time
import urllib2
from collections import OrderedDict
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from ldap_utilities import Ldap_Utilities
from unidecode import unidecode
from StringIO import StringIO
from tempfile import mkdtemp
from xml.dom.minidom import parseString, parse
from xml.etree import ElementTree
from lxml import etree
from zipfile import ZipFile

# 3rd party modules
import posix_ipc

# Same package
from artusplugin import util, model, _
from artusplugin.form import TicketForm
import artusplugin
import trac


class Ticket_Cache(object):
    """ This class and its subclasses are used for handling the working copy

    Objects of this class should not be instantiated directly.
    """

    @staticmethod
    def get_subclass(ticket_type):
        """ Return subclass associated to ticket type """
        return cacheddocument_subclasses[ticket_type]

    def __init__(self, env, trac_env_name, authname, ticket):
        self.env = env
        self.log_level = env.config.get('logging', 'log_level')
        self.trac_env_name = trac_env_name
        self.authname = authname
        self.ticket_type = ticket['type']
        self.ci_name = ticket['configurationitem']
        self.ticket_summary = ticket['summary']
        self.ticket_creationdate = ticket['time'].strftime('%d/%m/%Y')
        self.version_id = ticket['versionsuffix'].lstrip('_')
        self.id = ticket.id
        self.sourceurl = ticket['sourceurl']
        self.revision = util.get_revision(self.sourceurl)
        if not self.revision:
            self.revision = 'HEAD'
        self.repo_url = '%s@%s' % (util.get_repo_url(env, util.get_url(self.sourceurl)),
                                   self.revision)
        self.path = '/var/cache/trac/tickets/%s/%s/%s/%s/t%s%s' % (
            self.trac_env_name, self.authname, self.ticket_type,
            self.ci_name, self.id, util.get_url(self.sourceurl))
        self.sem_rel_path = ""
        match = re.match(r'((?:/\w+)?/(?:trunk|tags|branches)(?:/B\d+)?)'
                         r'(?:/.+)', util.get_url(self.sourceurl))
        if match:
            self.sem_rel_path = match.group(1)
        self.sem_abs_path = ""
        if self.sem_rel_path:
            self.sem_abs_path = '/var/cache/trac/tickets/%s/%s/%s/%s/t%s%s' % (
                self.trac_env_name, self.authname, self.ticket_type,
                self.ci_name, self.id, self.sem_rel_path)
        self.sem_name = '/%s' % str(self.sem_abs_path.replace('/', ':'))
        self.cache_unzip = '%s/.unzip' % self.path
        self.form_filename = '%s/trac_data.xml' % self.path
        self.vbaprojectfile = 'word/vbaProject.bin'
        self.vbasignaturefile = 'word/vbaProjectSignature.bin'
        self.vbaprojectfiles = 'word/vbaProject*.bin'
        self.vbadatafile = 'word/vbaData.xml'
        self.schemas_url = self.env.config.get('artusplugin', 'schemas_url')
        self.schemas_url_legacy_list = [url.strip() for url in env.config.get("artusplugin", "schemas_url_legacy_list").split(',')]
        sha1 = hashlib.sha1()
        sha1.update(util.get_url(self.sourceurl).encode('utf-8'))
        self.sourceurl_hash = format(sha1.hexdigest())
        self.sem_handle = posix_ipc.Semaphore(
            name=self.sem_name,
            flags=posix_ipc.O_CREAT,
            initial_value=1)

    def __enter__(self):
        self.sem_handle.acquire()
        try:
            if not self.exist_wc():
                self.create_wc()
        except Exception as e:
            self.sem_handle.release()
            raise TracError(e)

        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.sem_handle.release()

    def exist_wc(self):
        # Test existence of working copy
        return os.path.isdir("%s/.svn" % self.path)

    def create_wc(self):
        # Create working copy
        unix_cmd_list = ['mkdir -p "%s"' % self.path]
        unix_cmd_list += [util.SVN_TEMPLATE_CMD % {'subcommand': 'co --depth empty'} +
                          '"' + self.repo_url + '" "' + self.path + '"']
        retcode, lines = util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())
        if retcode != 0:
            raise TracError('\n'.join(lines))

    def update_wc(self):
        # Update working copy
        unix_cmd_list = [util.SVN_TEMPLATE_CMD % {'subcommand': 'up'} +
                         '"' + self.path + '"']
        retcode, lines = util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())
        if retcode != 0:
            raise TracError('\n'.join(lines))

    def switch_wc(self, url):
        # Switch working copy
        unix_cmd_list = [util.SVN_TEMPLATE_CMD % {'subcommand': 'switch'} +
                         '--ignore-ancestry "' + url + '" "' + self.path + '"']
        retcode, lines = util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())
        if retcode != 0:
            raise TracError('\n'.join(lines))

    def add(self, docfile):
        if docfile:
            if not self.exist_in_repo(docfile, 'HEAD'):
                if not self.exist_in_wc(docfile) and docfile.endswith('.docm'):
                    # Source template
                    docpath = '%s/%s' % (self.path, docfile)
                    shutil.copy(self.template, docpath)
                if not self.exist_in_wc(docfile) and docfile.endswith('.pdf'):
                    # PDF template
                    docpath = '%s/%s' % (self.path, docfile)
                    open(docpath, 'a').close()
                if self.exist_in_wc(docfile):
                    unix_cmd_list = ['cd "%s";%s' % (self.path, util.SVN_TEMPLATE_CMD % {'subcommand': 'add --force'} +
                                                '"' + docfile + '"')]
                    unix_cmd_list += ['cd "%s";%s' % (self.path, util.SVN_TEMPLATE_CMD % {'subcommand': 'propset --force svn:needs-lock "*"'} +
                                                    '"' + docfile + '"')]
                    retcode, lines = util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())
                    if retcode != 0:
                        raise TracError('\n'.join(lines))
                else:
                    raise TracError('File not found: %s' % docpath)
            else:
                self.checkout(docfile)

    def checkout(self, docfile):
        if docfile and self.exist_in_repo(docfile, self.revision):
            unix_cmd_list = ['cd "%s";%s' % (self.path, util.SVN_TEMPLATE_CMD % {'subcommand': 'up'} +
                                           '-r %s ' % self.revision  + '"' + docfile + '"')]
            retcode, lines = util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())
            if retcode != 0:
                raise TracError('\n'.join(lines))

    def update(self, docfile):
        if docfile and docfile != 'N/A':
            if not self.exist_in_repo(docfile, 'HEAD'):
                # File has been removed, WC directory must be updated
                unix_cmd_list = ['cd "%s";%s' % (self.path, util.SVN_TEMPLATE_CMD % {'subcommand': 'up'} +
                                               '"."')]
                retcode, lines = util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())
                if retcode != 0:
                    raise TracError('\n'.join(lines))
            else:
                unix_cmd_list = ['cd "%s";%s' % (self.path, util.SVN_TEMPLATE_CMD % {'subcommand': 'up'} +
                                               '"' + docfile + '"')]
                retcode, lines = util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())
                if retcode != 0:
                    raise TracError('\n'.join(lines))

    def exist_in_repo(self, docfile, revision):
        """
            Test existence of the given docfile in the repository at the given revision
        """
        if docfile:
            unix_cmd_list = [util.SVN_TEMPLATE_CMD % {'subcommand': 'info'} +
                             '"%s/%s@%s" &> /dev/null' % (
                             util.get_repo_url(self.env,
                                               util.get_url(self.sourceurl)),
                             docfile,
                             revision)]
            retcode = util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())[0]
            if retcode != 0:
                return False
            else:
                return True
        else:
            return False

    def exist_in_wc(self, docfile):
        """
            Test existence of the given docfile in the working copy
        """
        if docfile:
            return os.path.isfile('%s/%s' % (self.path, docfile))
        else:
            return False

    def status(self, docfile, status):
        """
            Gets back data on working copy status (status = 'wc-status')
            or repository status (status = 'repos-status') regarding:
            -> modifications => 'change_status'
            -> locks => 'lock_agent', 'lock_client'
            on the given docfile
        """
        change_status = None
        lock_agent = None
        lock_ticket = None
        lock_client = None
        if (docfile and
            ((status == 'wc-status' and self.exist_in_wc(docfile)) or
             (status == 'repos-status' and self.exist_in_repo(docfile, 'HEAD')))):
            # missing pristine warning eg is filtered out
            unix_cmd = util.SVN_TEMPLATE_CMD % {
                'subcommand': 'status --xml --show-updates --verbose'} + \
                '"%s/%s" 2> /dev/null' % (self.path, docfile)
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
                            re_client = _('ticket:(\d+) \(on behalf of ([^)]+)\)')
                            match = re.search(re_client, comment_list[0].childNodes[0].data)
                            if match:
                                lock_ticket = match.group(1)
                                lock_client = match.group(2)
        return {'change_status': change_status,
                'lock_agent': lock_agent,
                'lock_ticket': lock_ticket,
                'lock_client': lock_client}

    def lock(self, docfile):
        if docfile and self.exist_in_wc(docfile):
            wc_status = self.status(docfile, 'wc-status')
            if wc_status['change_status'] in ('added', 'unversioned'):
                return
            repos_status = self.status(docfile, 'repos-status')
            if repos_status['lock_agent']:
                if repos_status['lock_agent'] != 'trac':
                    raise TracError("Sorry, the file %s is already locked outside of trac by %s"
                                    % (docfile, repos_status['lock_agent']))
                elif wc_status['lock_agent'] is None:
                    raise TracError("Sorry, the file %s is already locked by %s with ticket #%s"
                                    % (docfile, repos_status['lock_client'], repos_status['lock_ticket']))
                else:
                    if self.log_level == 'INFO':
                        syslog.syslog("lock was already set in the working copy")
            else:
                unix_cmd_list = ['cd "%s";%s' % (self.path, util.SVN_TEMPLATE_CMD % {'subcommand': 'lock --force -m "%s" "%s"' % (
                    _('ticket:%(id)s (on behalf of %(user)s)', id=str(self.id), user=self.authname),
                    docfile)})]
                retcode, lines = util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())
                if retcode != 0:
                    raise TracError('\n'.join(lines))
                else:
                    self.set_access(docfile)

    def unlock(self, docfile):
        if docfile and self.exist_in_wc(docfile):
            wc_status = self.status(docfile, 'wc-status')
            if wc_status['change_status'] in ('added', 'unversioned'):
                return
            if (not wc_status['lock_agent']):
                if self.log_level == 'INFO':
                    syslog.syslog("lock was not set in the working copy")
            else:
                unix_cmd_list = ['cd "%s";%s' % (self.path, util.SVN_TEMPLATE_CMD % {'subcommand': 'unlock --force'} +
                                               '"' + docfile + '"')]
                retcode, lines = util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())
                if retcode != 0:
                    raise TracError('\n'.join(lines))
                else:
                    self.set_access(docfile)

    def get_flag_rel_path(self, docfile):
        """
            Gets back pdf generation flag path relative to ticket path
        """
        pdf_flag_rel_path = ""
        if docfile:
            pdf_flag_rel_path = util.get_url(self.sourceurl)

        return pdf_flag_rel_path

    def get_flag_abs_path(self, docfile):
        """
            Gets back pdf generation flag absolute path
        """
        pdf_flag_abs_path = ""
        if docfile:
            pdf_flag_rel_path = self.get_flag_rel_path(docfile)
            if pdf_flag_rel_path:
                pdf_flag_abs_path = '/var/cache/trac/tickets/%s/%s/%s/%s/t%s%s' % (
                    self.trac_env_name, self.authname, self.ticket_type,
                    self.ci_name, self.id, pdf_flag_rel_path)

        return pdf_flag_abs_path

    def exist_flag(self, docfile, suffix):
        """
            Test existence of pdf generation flag
        """
        if docfile:
            pdf_flag_abs_path = self.get_flag_abs_path(docfile)
            if pdf_flag_abs_path:
                flag_file = '%s/%s.%s' % (pdf_flag_abs_path, docfile, suffix)
                if os.path.exists(flag_file):
                    return True
                else:
                    return False

    def create_flag(self, docfile, suffix):
        """
            Create pdf generation flag
        """
        if docfile:
            pdf_flag_abs_path = self.get_flag_abs_path(docfile)
            if pdf_flag_abs_path:
                flag_file = '%s/%s.%s' % (pdf_flag_abs_path, docfile, suffix)
                if not os.path.exists(flag_file):
                    open(flag_file, 'a').close()

    def remove_flag(self, docfile, suffix):
        """
            Remove pdf generation flag
        """
        if docfile:
            pdf_flag_abs_path = self.get_flag_abs_path(docfile)
            if pdf_flag_abs_path:
                flag_file = '%s/%s.%s' % (pdf_flag_abs_path, docfile, suffix)
                if os.path.exists(flag_file):
                    os.remove(flag_file)

    def commit(self):
        # Set svn:ignore property
        my_ignore_list = ['.ignore', '.unzip', '.sign', 'trac_data.xml']
        theirs_ignore_list = []
        unix_cmd_list = [util.SVN_TEMPLATE_CMD % {'subcommand': 'propget svn:ignore'} + '"' + self.path + '" &> /dev/null']
        retcode = util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())[0]
        if retcode == 0:
            unix_cmd_list = [util.SVN_TEMPLATE_CMD % {'subcommand': 'propget svn:ignore'} + '"' + self.path + '"']
            lines = util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())[1]
            theirs_ignore_list = [line.strip('\n') for line in lines if line != '\n']
        ignore_string = '\n'.join(theirs_ignore_list + [item for item in my_ignore_list if item not in theirs_ignore_list])
        unix_cmd_list = ['echo -en "' + ignore_string + '" ' + '> "' + self.path + '/.ignore"']
        unix_cmd_list += [util.SVN_TEMPLATE_CMD % {'subcommand': 'propset svn:ignore -F "' +
                          self.path + '/.ignore"'} + '"' + self.path + '"']
        unix_cmd_list += ['cd "%s";%s' % (self.path, util.SVN_TEMPLATE_CMD % {'subcommand': 'commit -m "%s"' %
                            _('ticket:%(id)s (on behalf of %(user)s)', id=str(self.id), user=self.authname)})]
        retcode, lines = util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())
        if retcode != 0:
            raise TracError('\n'.join(lines))
        else:
            revision = ''
            for line in lines:
                if line.startswith(u'Révision '):
                    regular_expression = u'\ARévision (\d+) propagée\.\n\Z'
                    match = re.search(regular_expression, line)
                    if match:
                        revision = match.group(1)
                    break
        return revision

    def get_customxml_filepath(self, dir = None):
        if not dir:
            dir = self.cache_unzip
        item_dirpath = '%s/customXml' % dir
        for item_filename in fnmatch.filter(os.listdir(item_dirpath), 'item[0-9].xml'):
            item_filepath = "%s/%s" % (item_dirpath, item_filename)
            customxml_root_elt = ElementTree.parse(item_filepath).getroot()
            if customxml_root_elt.tag in [self.root_tag] + self.root_tag_legacy_list:
                return item_filepath
        else:
            message = tag.span('%s: The custom XML could not be identified in the following directory:' % util.lineno())
            message(tag.p(item_dirpath))
            raise TracError(message, 'Hostname mismatch ?', True)

    def get_customxml_root_elt(self, file_path):
        input_zip = ZipFile(file_path)
        items = {name: input_zip.read(name) for name in input_zip.namelist() if re.match('customXml/item\d+\.xml', name)}
        for customxml_content in items.values():
            customxml_root_elt = ElementTree.fromstring(customxml_content)
            if customxml_root_elt.tag in [self.root_tag] + self.root_tag_legacy_list:
                return customxml_root_elt

    def get_pdffile_list(self):
        return [f for f in os.listdir(self.path) if f.endswith('.pdf')]

    def get_webdav_url(self, docfile, action='edit'):
        webdav_url = ""
        if docfile:
            clickonce_app_url = self.env.config.get('artusplugin', 'clickonce_app_url_MS_Office')
            suffix = docfile.split('.')[-1]
            scheme = self.env.config.get('artusplugin', 'scheme')
            webdav_protocol = util.get_prop_values(self.env, 'webdav_protocol')[suffix]
            if scheme == 'https':
                webdav_protocol += 's'
            webdav_url = '%s?action=%s&mode=webdav&url=%s://%s/tickets/%s/%s/%s/%s/t%s%s/%s/%s' % (
                clickonce_app_url,
                action,
                webdav_protocol,
                util.get_hostname(self.env),
                self.trac_env_name,
                self.authname,
                self.ticket_type,
                self.ci_name,
                self.id,
                self.sem_rel_path,
                self.sourceurl_hash,
                unicode_quote(docfile))

        return webdav_url

    def get_http_url(self, docfile):
        http_url = ""
        if docfile:
            scheme = self.env.config.get('artusplugin', 'scheme')
            http_url = '%s://%s/tickets/%s/%s/%s/%s/t%s%s/%s/%s' % (
                scheme,
                util.get_hostname(self.env),
                self.trac_env_name,
                self.authname,
                self.ticket_type,
                self.ci_name,
                self.id,
                self.sem_rel_path,
                self.sourceurl_hash,
                unicode_quote(docfile))

        return http_url

    def get_publish_url(self, srcfile, pdffile):
        """ Cases where srcfile and/or pdffile are not the expected values
            are handled by clickonce app, in order to commit in any event """
        clickonce_app_url = self.env.config.get('artusplugin', 'clickonce_app_url_MS_Office')
        scheme = self.env.config.get('artusplugin', 'scheme')
        publish_url = ('%s?action=publish'
                      '&url=%s://%s/tickets/%s/%s/%s/%s/t%s%s/%s'
                      '&srcfile=%s'
                      '&pdffile=%s') % (
            clickonce_app_url,
            scheme,
            util.get_hostname(self.env),
            self.trac_env_name,
            self.authname,
            self.ticket_type,
            self.ci_name,
            self.id,
            self.sem_rel_path,
            self.sourceurl_hash,
            unicode_quote(srcfile),
            unicode_quote(pdffile))

        return publish_url

    def get_size(self, docfile):
        if docfile and self.exist_in_wc(docfile):
            return os.stat('%s/%s' % (self.path, docfile)).st_size

    def get_mtime(self, docfile):
        if docfile and self.exist_in_wc(docfile):
            return os.stat('%s/%s' % (self.path, docfile)).st_mtime

    def set_access(self, docfile):
        if docfile and self.exist_in_wc(docfile):
            repos_status = self.status(docfile, 'wc-status')
            authz = 'granted' if repos_status['lock_agent'] == 'trac' else 'denied'
            htaccess_lines = [
                '<Files "%s">' % docfile,
                '  <LimitExcept GET HEAD OPTIONS PROPFIND>',
                '    Require all %s' % authz,
                '  </LimitExcept>',
                '  <Limit GET HEAD OPTIONS PROPFIND>',
                '    Require all granted',
                '  </Limit>',
                '</Files>']
            docfile_ext = os.path.splitext(docfile)[1]
            htaccess_name = '.htaccess.pdf' if docfile_ext == '.pdf' else '.htaccess.source'
            htaccess_path = '%s/%s' % (self.path, htaccess_name)
            with open(htaccess_path, 'w') as htaccess_file:
                htaccess_file.writelines(line + '\n' for line in htaccess_lines)

    def unzip(self, docfile):
        """ Open docm """
        if docfile and docfile.endswith('.docm') and self.exist_in_wc(docfile):
            unix_cmd_list = ['rm -Rf "%s";mkdir -p "%s"' % (
                self.cache_unzip, self.cache_unzip)]
            # Using "7za" instead of "unzip" that seems to be not working properly according to:
            # https://forums.centos.org/viewtopic.php?t=5451
            # "unzip" reporting CRC errors on large files
            unix_cmd_list += ['7za e -spf -o"%s" "%s/%s"' % (
                 self.cache_unzip, self.path, docfile)]
            retcode, lines = util.unix_cmd_apply(self.env, unix_cmd_list,
                                                 util.lineno())
            if retcode != 0:
                raise TracError('\n'.join(lines))

    def zip(self, docfile):
        """ Close docm """
        if docfile and docfile.endswith('.docm') and self.exist_in_wc(docfile):
            unix_cmd_list = ['rm -f "%s/%s"' % (
                self.path, docfile)]
            # Used "zip" command because could not get it working with 7za
            unix_cmd_list += ['cd "%s";zip -r "%s/%s" . -i \*' % (
                self.cache_unzip, self.path, docfile)]
            retcode, lines = util.unix_cmd_apply(self.env, unix_cmd_list,
                                                 util.lineno())
            if retcode != 0:
                raise TracError('\n'.join(lines))

    def upgrade_document(self, docfile):
        """ Upgrade document from newer template
            (limited to word document)
        """
        if docfile and docfile.endswith('.docm') and self.exist_in_wc(docfile) and self.template:
            self.unzip(docfile)

            document_upgraded = False

            # Namespace is updated if needed
            for line, new in util.inplace(self.get_customxml_filepath()):
                for schemas_url_legacy in self.schemas_url_legacy_list:
                    if schemas_url_legacy in line:
                        line = line.replace(schemas_url_legacy, self.schemas_url)
                        break
                new.write(line)

            doc_customxml_dom = parse(self.get_customxml_filepath())
            try:
                # Get the current template reference (without edition)
                template_ref = os.path.splitext(os.path.basename(self.template))[0]
                # Get the document template reference (including edition)
                doc_elt = doc_customxml_dom.getElementsByTagName("ns0:TemplateRef")[0]
                doc_template_ref = doc_elt.firstChild.nodeValue
                # Get the current edition ultimate pointed to path, where all editions are located
                template_path = os.path.realpath(self.template)
                # Get the compatible template for providing an upgraded VBA code
                template_path = re.sub(r"%s_E[0-9]+" % template_ref, doc_template_ref, template_path)
            except IndexError:
                # Get current edition symbolic link target path
                template_path = os.readlink(self.template)
                # Get default edition template path for providing an upgraded VBA code
                template_path = re.sub(r"current", r"default", template_path)
                # Get default edition ultimate pointed to path
                template_path = os.path.realpath(template_path)
            if not os.path.exists(template_path):
                # No upgrade
                return

            # Check if the VBA code has been upgraded
            doc_elt = doc_customxml_dom.getElementsByTagName("ns0:Template")[0]
            doc_mt = doc_elt.getAttribute("ModificationDateTime")
            tmpl_customxml_root_elt = self.get_customxml_root_elt(template_path)
            tmpl_elt = tmpl_customxml_root_elt.find('{%s}Template' % self.ns0)

            if tmpl_elt:
                tmpl_mt = tmpl_elt.attrib['ModificationDateTime']

                if tmpl_mt > doc_mt:
                    cmd = ('cd %s;'
                           '/usr/bin/rm -f %s %s;'
                           '/usr/bin/unzip "%s" '
                           '"%s" %s' % (
                               self.cache_unzip,
                               self.vbaprojectfiles,
                               self.vbadatafile,
                               template_path,
                               self.vbaprojectfiles,
                               self.vbadatafile
                               ))
                    retcode = subprocess.call(cmd, shell=True)
                    if retcode != 0:
                        raise TracError("Could not upgrade VBA project "
                                        "of current document - "
                                        "unzip return code: %s" % retcode)
                    else:
                        document_upgraded = True
                        syslog.syslog("%s(%s): VBA project upgraded from template '%s' - ticket %s (%s)" %
                                      (self.trac_env_name, self.authname, template_path, self.id, self.ticket_type))

                        customxml_tree = ElementTree.parse(self.get_customxml_filepath())
                        customxml_tmpl = customxml_tree.find('{%s}Template' % self.ns0)

                        if customxml_tmpl:
                            customxml_tmpl.set('ModificationDateTime', tmpl_mt)
                            customxml_tree.write(self.get_customxml_filepath())
                            syslog.syslog("%s(%s): Modification DateTime updated - ticket %s (%s)" %
                                          (self.trac_env_name, self.authname, self.id, self.ticket_type))
                        else:
                            message = tag.span('%s: Template not found or empty in template file:' % util.lineno())
                            message(tag.p(self.get_customxml_filepath()))
                            raise TracError(message, 'Hostname mismatch ?', True)

                        for xml_file in ['document.xml'] + fnmatch.filter(os.listdir('%s/word' % self.cache_unzip), 'footer[1-99].xml'):
                            xml_filepath = '%s/word/%s' % (self.cache_unzip, xml_file)
                            xml_tree = etree.parse(xml_filepath)
                            xml_root = xml_tree.getroot()
                            xml_ns = xml_root.nsmap['w']
                            for data_binding in xml_tree.findall('.//w:dataBinding', xml_root.nsmap):
                                prefix_mappings = data_binding.get('{%s}prefixMappings' % xml_ns)
                                if prefix_mappings:
                                    for schemas_url_legacy in self.schemas_url_legacy_list:
                                        if schemas_url_legacy in prefix_mappings:
                                            prefix_mappings = prefix_mappings.replace(schemas_url_legacy, self.schemas_url)
                                            break
                                    data_binding.set('{%s}prefixMappings' % xml_ns, prefix_mappings)
                            xml_tree.write(xml_filepath)
                        syslog.syslog("%s(%s): Databinding upgraded - ticket %s (%s)" %
                                      (self.trac_env_name, self.authname, self.id, self.ticket_type))
            else:
                message = tag.span('%s: Template not found or empty in template file:' % util.lineno())
                message(tag.p(template_path))
                raise TracError(message, 'Hostname mismatch ?', True)

            if document_upgraded:
                self.zip(docfile)

    def get_version_status(self, docfile):
        """ Return document version status
            (limited to word document)
        """
        version_status = self.version_status
        if docfile and docfile.endswith('.docm') and self.exist_in_wc(docfile):
            self.unzip(docfile)
            customxml_tree = ElementTree.parse(self.get_customxml_filepath())
            if customxml_tree:
                for ns0 in [self.ns0] + self.ns0_legacy_list:
                    customxml_id = customxml_tree.find('{%s}Identification' % ns0)
                    if customxml_id is not None and len(customxml_id):
                        version_status = customxml_id.find('{%s}VersionStatus' % ns0).text
                        if version_status is None:
                            version_status = ''
                        break
                else:
                    syslog.syslog("Identification not found or empty in custom xml file")

        return version_status

    @staticmethod
    def lock_description(src_file):
        """ Set Lock description depending upon src_file/pdf_file combination
        """

        if src_file != 'N/A':
            lock_description = "Setup Source File for Edit"
        else:
            lock_description = ""

        return lock_description

    @staticmethod
    def unlock_description(src_file, pdf_file):
        """ Set Unlock description depending upon src_file/pdf_file combination
        """

        if src_file == 'N/A' and pdf_file != 'N/A':
            unlock_description = "Commit PDF File"
        elif pdf_file == 'N/A' and src_file != 'N/A':
            unlock_description = "Commit Source File"
        elif src_file != 'N/A' and pdf_file != 'N/A':
            if src_file.endswith('.docm'):
                unlock_description = "Finalize Source File, export as PDF File and commit both"
            else:
                unlock_description = "Commit Source and PDF Files"
        else:
            unlock_description = ""

        return unlock_description


class ECM_Cache(Ticket_Cache):

    def __init__(self, env, trac_env_name, authname, ticket):
        super(ECM_Cache, self).__init__(env, trac_env_name, authname, ticket)
        self.template = None
        template_fn = self.env.config.get('artusplugin', 'ECM_template')
        if template_fn:
            for template_dir in Chrome(self.env).get_templates_dirs():
                template = '%s/%s' % (template_dir, template_fn)
                if os.path.isfile(template):
                    self.template = template
                    break
            else:
                raise TracError('Unable to find ECM template')
        self.ns0 = '%s/ECM' % self.schemas_url
        self.ns0_legacy_list = ["%s/%s" % (url, 'ECM') for url in self.schemas_url_legacy_list]
        self.root_tag = "{%s}ECM" % self.ns0
        self.root_tag_legacy_list = ["{%s}%s" % (ns0, 'ECM') for ns0 in self.ns0_legacy_list]
        attachments = []
        if ticket['ecmtype'] == 'Technical Note':
            attachments.extend(["    Size    Name", "  --------  ---------"])
            attachments.extend(["  %8d  %s" % (attachment.size, attachment.filename)
                                for attachment in Attachment.select(self.env, 'ticket', ticket.id)])
        else:
            for archive_path in PDFPackage.get_archives_paths(ticket):
                fileSize = os.path.getsize(archive_path)
                attachments.append("\r\n%s (%s):\r\n" % (os.path.basename(archive_path), pretty_size(fileSize)))
                attachments.extend(["    CRC     Path/Name", "  --------  ---------"])
                with ZipFile(archive_path, 'r') as archive:
                    attachments.extend(["  %08X  %s" % (zipinfo.CRC, zipinfo.filename)
                                        for zipinfo in archive.infolist()
                                        if not zipinfo.filename.endswith('/')])
        self.trac_data = {'Identification': {'CreationDate': self.ticket_creationdate,
                                             'DocumentName': self.ticket_summary,
                                             'DocumentSubject': ticket['keywords'],
                                             'Program': self.trac_env_name,
                                             'ECMType': ticket['ecmtype']},
                          'Appendix': {'Attachments': '\r\n'.join(attachments)}}
        self.distribution = {'From': '%s\n%s\n%s' % (ticket['fromname'],
                                                     ticket['fromemail'],
                                                     ticket['fromphone']),
                             'To': '%s\n%s\n%s' % (ticket['toname'],
                                                   ticket['toemail'],
                                                   ticket['tophone']),
                             'Copy': ticket['carboncopy']}

    def fill_pdf(self, pdffile):

        # Create sub-directory for filling in the form
        pdf_dir = "%s/.pdf" % self.path
        if not os.path.exists(pdf_dir):
            os.makedirs(pdf_dir)

        src = "%s/%s" % (self.path, pdffile)
        dest = '%s/%s' % (pdf_dir, pdffile)

        app_properties_path = "/srv/trac/common/TracPdfFill.properties"

        # Fill in form fields
        unix_cmd_list = ['sudo /usr/bin/java '
                         '-jar /srv/trac/common/TracPdfFill.jar'
                         ' -P "%s"'
                         ' -a "%s"'
                         ' -t "%s" "%s" "%s"'
                         ' -s "%s"'
                         ' -d "%s"' % (
                             app_properties_path,
                             "FillFields",
                             self.distribution['From'],
                             self.distribution['To'],
                             self.distribution['Copy'],
                             src,
                             dest)]

        # Copy modified PDF to the working copy (if not empty)
        unix_cmd_list += ['if [[ -s "%s" ]] ; then cp -f "%s" "%s"; fi'
                          % (dest, dest, src)]

        retcode, lines = util.unix_cmd_apply(self.env, unix_cmd_list,
                                             util.lineno())
        if retcode != 0:
            raise TracError('\n'.join(lines))

    def get_dist_rectangles(self, docfile):
        """ Return document distribution rectangles coordinates
        """
        # Extract sign rectangles from source file
        self.unzip(docfile)
        customxml_filename = self.get_customxml_filepath()
        customxml_tree = ElementTree.parse(customxml_filename)
        if customxml_tree:
            for ns0 in [self.ns0] + self.ns0_legacy_list:
                customxml_dist = next(iter(customxml_tree.getiterator('{%s}Distribution' % ns0)), None)
                if customxml_dist is not None and len(customxml_dist):
                    customxml_xvalues = customxml_dist.getiterator('{%s}XValue' % ns0)
                    customxml_yvalues = customxml_dist.getiterator('{%s}YValue' % ns0)
                    xy_list = zip(customxml_xvalues, customxml_yvalues)
                    for i in range(0, len(xy_list), 2):
                        rect = xy_list[i:i + 2]
                        yield tuple(rect)

    def get_sign_rectangles(self, docfile):
        """ Return document sign rectangles coordinates
        """
        # Extract sign rectangles from source file
        self.unzip(docfile)
        customxml_filename = self.get_customxml_filepath()
        customxml_tree = ElementTree.parse(customxml_filename)
        if customxml_tree:
            for ns0 in [self.ns0] + self.ns0_legacy_list:
                customxml_sign = next(iter(customxml_tree.getiterator('{%s}Signatures' % ns0)), None)
                if customxml_sign is not None and len(customxml_sign):
                    customxml_xvalues = customxml_sign.getiterator('{%s}XValue' % ns0)
                    customxml_yvalues = customxml_sign.getiterator('{%s}YValue' % ns0)
                    xy_list = zip(customxml_xvalues, customxml_yvalues)
                    for i in range(0, len(xy_list), 2):
                        rect = xy_list[i:i + 2]
                        yield tuple(rect)
        else:
            yield

    def create_wc(self):
        # Create repository url if it does not exist yet
        unix_cmd_list = [util.SVN_TEMPLATE_CMD % {'subcommand': 'mkdir -m "%s" --parents "%s" &> /dev/null' % (
            _('ticket:%(id)s (on behalf of %(user)s)', id=str(self.id), user=self.authname),
            util.get_url(self.repo_url))}]
        util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())
        # Create working copy
        super(ECM_Cache, self).create_wc()

    def update_data(self, docfile, force=False):
        """ Update document data
            (limited to word document)
        """
        if docfile and docfile.endswith('.docm') and self.exist_in_wc(docfile):
            self.unzip(docfile)
            # Prepare trac-data.xml
            self.form_data = SourceData(self.env,
                                        self.form_filename,
                                        self.schemas_url,
                                        'ns0:ECM',
                                        dict(zip(['ns0:%s' % group for group in self.trac_data.keys()],
                                                 [[['ns0:%s' % data for data in group_data.keys()]]
                                                  for group_data in self.trac_data.values()]))
                                        )

            for group in self.trac_data.keys():
                for data in self.trac_data[group].keys():
                    self.form_data.trac_data['ns0:%s' % group].data[0]['ns0:%s' % data] = self.trac_data[group][data]

            self.form_data.write(self.form_data.toxml)
            update_needed = False

            # Namespace is updated if needed
            for line, new in util.inplace(self.get_customxml_filepath()):
                for schemas_url_legacy in self.schemas_url_legacy_list:
                    if schemas_url_legacy in line:
                        line = line.replace(schemas_url_legacy, self.schemas_url)
                        update_needed = True
                        break
                new.write(line)

            # update of document Identification and Appendix only if needed
            # trac_data.xml replaces the Identification and Appendix part of the custom XML
            # if one part of those has changed
            for group in self.trac_data.keys():
                customxml_tree = ElementTree.parse(self.get_customxml_filepath())
                if customxml_tree:
                    customxml_root = customxml_tree.getroot()
                    customxml_root_tag = customxml_root.tag
                    for ns0 in [self.ns0] + self.ns0_legacy_list:
                        if ns0 in customxml_root_tag:
                            customxml_id = customxml_tree.find('{%s}%s' % (ns0, group))
                            if customxml_id is not None and len(customxml_id):
                                for data in self.trac_data[group].keys():
                                    customxml_text = customxml_id.find('{%s}%s' % (ns0, data)).text or ''
                                    trac_data_text = self.form_data.trac_data['ns0:%s' % group].data[0]['ns0:%s' % data]
                                    if customxml_text != trac_data_text.replace('\r\n', '\n'):
                                        data_change = True
                                        break
                                else:
                                    data_change = False
                                if data_change:
                                    customxml_root.remove(customxml_id)
                                    trac_data_tree = ElementTree.parse(self.form_filename)
                                    trac_data_id = trac_data_tree.find('{%s}%s' % (self.ns0, group))
                                    customxml_root.append(trac_data_id)
                                    customxml_tree.write(self.get_customxml_filepath())
                                    update_needed = True
                            else:
                                message = tag.span('%s: %s not found or empty in custom xml file' % (util.lineno(), group))
                                message(tag.p(self.get_customxml_filepath()))
                                raise TracError(message, 'Hostname mismatch ?', True)
                else:
                    message = tag.span('%s: Error parsing custom xml file:' % util.lineno())
                    message(tag.p(self.get_customxml_filepath()))
                    raise TracError(message, 'Hostname mismatch ?', True)

            if update_needed:
                self.zip(docfile)

    def xform_pdf(self, sourcefile, pdffile):

        # Create sub-directory for transforming the PDF into a form
        pdf_dir = "%s/.pdf" % self.path
        if not os.path.exists(pdf_dir):
            os.makedirs(pdf_dir)

        src = "%s/%s" % (self.path, pdffile)
        dest = '%s/%s' % (pdf_dir, pdffile)

        app_properties_path = "/srv/trac/common/TracPdfFill.properties"
        tmpl_properties_path = "%s/.pdf/TemplateECM.properties" % self.path

        # Form fields rectangles coordinates
        with open(tmpl_properties_path, 'w') as f:
            f.write("# TemplateECM properties\n")
            f.write("# sign rectangles (lower left x, lower left y) "
                    "(upper right x, upper right y)\n")
            dist_rectangles = self.get_dist_rectangles(sourcefile)
            for i, ((llXValue, llYValue),
                    (urXValue, urYValue)) in enumerate(dist_rectangles,
                                                       start=1):
                f.write("fieldrectangle%s=%s\n" % (i, llXValue.text))
                f.write("fieldrectangle%s=%s\n" % (i, llYValue.text))
                f.write("fieldrectangle%s=%s\n" % (i, urXValue.text))
                f.write("fieldrectangle%s=%s\n" % (i, urYValue.text))

        # Create form fields in the PDF
        unix_cmd_list = ['sudo /usr/bin/java '
                         '-jar /srv/trac/common/TracPdfFill.jar'
                         ' -P "%s"'
                         ' -p "%s"'
                         ' -a "%s"'
                         ' -s "%s"'
                         ' -d "%s"' % (
                             app_properties_path,
                             tmpl_properties_path,
                             "CreateFields",
                             src,
                             dest)]

        # Copy modified PDF to the working copy (if not empty)
        unix_cmd_list += ['if [[ -s "%s" ]] ; then cp -f "%s" "%s"; fi'
                          % (dest, dest, src)]

        retcode, lines = util.unix_cmd_apply(self.env, unix_cmd_list,
                                             util.lineno())
        if retcode != 0:
            raise TracError('\n'.join(lines))


class FEE_Cache(Ticket_Cache):

    def __init__(self, env, trac_env_name, authname, ticket):
        super(FEE_Cache, self).__init__(env, trac_env_name, authname, ticket)
        self.template = None
        template_fn = self.env.config.get('artusplugin', 'FEE_template')
        if template_fn:
            for template_dir in Chrome(self.env).get_templates_dirs():
                template = '%s/%s' % (template_dir, template_fn)
                if os.path.isfile(template):
                    self.template = template
                    break
            else:
                raise TracError('Unable to find FEE template')
        self.ns0 = '%s/FEE' % self.schemas_url
        self.ns0_legacy_list = ["%s/%s" % (url, 'FEE') for url in self.schemas_url_legacy_list]
        self.root_tag = "{%s}FEE" % self.ns0
        self.root_tag_legacy_list = ["{%s}%s" % (ns0, 'FEE') for ns0 in self.ns0_legacy_list]
        match = re.match(r'FEE_%s_(\d{5}-\d{2})_v(\d+)' % self.trac_env_name, self.ticket_summary)
        if match:
            self.fee_number = match.group(1)
            self.version_number = match.group(2)
        else:
            self.fee_number = "00001-00"
            self.version_number = "1"
        self.trac_data = {'Identification': {'FEENumber': self.fee_number,
                                             'VersionNumber': self.version_number,
                                             'CustomerName': ticket['customer'],
                                             'ProgramName': ticket['program'],
                                             'EquipmentName': ticket['application']},
                          'Configuration': [{'Name': item[0],
                                             'PN': item[1],
                                             'Amdt': item[2]}
                                             for item in util.SqlServerView().get_items(
                                                self.fee_number,
                                                ticket['customer'],
                                                ticket['program'],
                                                ticket['application'])]}

    def fill_pdf(self, pdffile):

        # Create sub-directory for filling in the form
        pdf_dir = "%s/.pdf" % self.path
        if not os.path.exists(pdf_dir):
            os.makedirs(pdf_dir)

        src = "%s/%s" % (self.path, pdffile)
        dest = '%s/%s' % (pdf_dir, pdffile)

        app_properties_path = "/srv/trac/common/TracPdfFill.properties"

        # Fill in form fields
        unix_cmd_list = ['sudo /usr/bin/java '
                         '-jar /srv/trac/common/TracPdfFill.jar'
                         ' -P "%s"'
                         ' -a "%s"'
                         ' -t "%s" "%s" "%s"'
                         ' -s "%s"'
                         ' -d "%s"' % (
                             app_properties_path,
                             "FillFields",
                             self.distribution['From'],
                             self.distribution['To'],
                             self.distribution['Copy'],
                             src,
                             dest)]

        # Copy modified PDF to the working copy (if not empty)
        unix_cmd_list += ['if [[ -s "%s" ]] ; then cp -f "%s" "%s"; fi'
                          % (dest, dest, src)]

        retcode, lines = util.unix_cmd_apply(self.env, unix_cmd_list,
                                             util.lineno())
        if retcode != 0:
            raise TracError('\n'.join(lines))

    def get_dist_rectangles(self, docfile):
        """ Return document distribution rectangles coordinates
        """
        # Extract sign rectangles from source file
        self.unzip(docfile)
        customxml_filename = self.get_customxml_filepath()
        customxml_tree = ElementTree.parse(customxml_filename)
        if customxml_tree:
            for ns0 in [self.ns0] + self.ns0_legacy_list:
                customxml_dist = next(iter(customxml_tree.getiterator('{%s}Distribution' % ns0)), None)
                if customxml_dist is not None and len(customxml_dist):
                    customxml_xvalues = customxml_dist.getiterator('{%s}XValue' % ns0)
                    customxml_yvalues = customxml_dist.getiterator('{%s}YValue' % ns0)
                    xy_list = zip(customxml_xvalues, customxml_yvalues)
                    for i in range(0, len(xy_list), 2):
                        rect = xy_list[i:i + 2]
                        yield tuple(rect)

    def get_sign_rectangles(self, docfile):
        """ Return document sign rectangles coordinates
        """
        # Extract sign rectangles from source file
        self.unzip(docfile)
        customxml_filename = self.get_customxml_filepath()
        customxml_tree = ElementTree.parse(customxml_filename)
        if customxml_tree:
            for ns0 in [self.ns0] + self.ns0_legacy_list:
                customxml_sign = next(iter(customxml_tree.getiterator('{%s}Signatures' % ns0)), None)
                if customxml_sign is not None and len(customxml_sign):
                    customxml_xvalues = customxml_sign.getiterator('{%s}XValue' % ns0)
                    customxml_yvalues = customxml_sign.getiterator('{%s}YValue' % ns0)
                    xy_list = zip(customxml_xvalues, customxml_yvalues)
                    for i in range(0, len(xy_list), 2):
                        rect = xy_list[i:i + 2]
                        yield tuple(rect)
        else:
            yield

    def create_wc(self):
        # Create repository url if it does not exist yet
        unix_cmd_list = [util.SVN_TEMPLATE_CMD % {'subcommand': 'mkdir -m "%s" --parents "%s" &> /dev/null' % (
            _('ticket:%(id)s (on behalf of %(user)s)', id=str(self.id), user=self.authname),
            util.get_url(self.repo_url))}]
        util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())
        # Create working copy
        super(FEE_Cache, self).create_wc()

    def update_data(self, docfile, force=False):
        """ Update document data
            (limited to word document)
        """
        if docfile and docfile.endswith('.docm') and self.exist_in_wc(docfile):
            self.unzip(docfile)
            # Prepare trac-data.xml
            self.form_data = SourceData(self.env,
                                        self.form_filename,
                                        self.schemas_url,
                                        'ns0:FEE',
                                        dict(zip(['ns0:%s' % group for group in self.trac_data.keys()],
                                                 [[['ns0:%s' % data for data in item.keys()]
                                                   for item in group_data]
                                                  if isinstance(group_data, list)
                                                  else
                                                  [['ns0:%s' % data for data in group_data.keys()]]
                                                  for group_data in self.trac_data.values()]))
                                        )

            for group in self.trac_data.keys():
                items = self.trac_data[group] if isinstance(self.trac_data[group], list) else [self.trac_data[group]]
                for index, item in enumerate(items):
                    for data in item.keys():
                        self.form_data.trac_data['ns0:%s' % group].data[index]['ns0:%s' % data] = item[data]

            self.form_data.write(self.form_data.toxml)
            update_needed = False

            # Namespace is updated if needed
            for line, new in util.inplace(self.get_customxml_filepath()):
                for schemas_url_legacy in self.schemas_url_legacy_list:
                    if schemas_url_legacy in line:
                        line = line.replace(schemas_url_legacy, self.schemas_url)
                        update_needed = True
                        break
                new.write(line)

            # update of Identification and Configuration only if needed
            # trac_data.xml replaces the Identification and Configuration parts of the custom XML
            # if one part of those has changed
            for group in self.trac_data.keys():
                customxml_tree = ElementTree.parse(self.get_customxml_filepath())
                if customxml_tree:
                    customxml_root = customxml_tree.getroot()
                    customxml_root_tag = customxml_root.tag
                    for ns0 in [self.ns0] + self.ns0_legacy_list:
                        if ns0 in customxml_root_tag:
                            customxml_id = customxml_tree.find('{%s}%s' % (ns0, group))
                            if customxml_id is not None and len(customxml_id):
                                trac_data_items = self.trac_data[group] if isinstance(self.trac_data[group], list) else [self.trac_data[group]]
                                if group == 'Identification':
                                    customxml_items = [customxml_id]
                                else:  # Configuration
                                    customxml_items = customxml_id.findall('{%s}%s' % (ns0, 'Item'))
                                if len(trac_data_items) != len(customxml_items):
                                    data_change = True
                                else:
                                    for index, (trac_data_item, customxml_item) in enumerate(zip(trac_data_items, customxml_items)):
                                        for data in trac_data_item.keys():
                                            trac_data_text = self.form_data.trac_data['ns0:%s' % group].data[index]['ns0:%s' % data]
                                            customxml_text = customxml_item.findtext('{%s}%s' % (ns0, data))
                                            if trac_data_text and customxml_text != trac_data_text.replace('\r\n', '\n'):
                                                data_change = True
                                                break
                                        else:
                                            data_change = False
                                if data_change:
                                    customxml_root.remove(customxml_id)
                                    trac_data_tree = ElementTree.parse(self.form_filename)
                                    trac_data_id = trac_data_tree.find('{%s}%s' % (self.ns0, group))
                                    customxml_root.append(trac_data_id)
                                    customxml_tree.write(self.get_customxml_filepath())
                                    update_needed = True
                            else:
                                message = tag.span('%s not found or empty in custom xml file' % group)
                                message(tag.p(self.get_customxml_filepath()))
                                raise TracError(message, 'Hostname mismatch ?', True)
                else:
                    message = tag.span('%s: Error parsing custom xml file:' % util.lineno())
                    message(tag.p(self.get_customxml_filepath()))
                    raise TracError(message, 'Hostname mismatch ?', True)

            if update_needed:
                self.zip(docfile)

    def xform_pdf(self, sourcefile, pdffile):

        # Create sub-directory for transforming the PDF into a form
        pdf_dir = "%s/.pdf" % self.path
        if not os.path.exists(pdf_dir):
            os.makedirs(pdf_dir)

        src = "%s/%s" % (self.path, pdffile)
        dest = '%s/%s' % (pdf_dir, pdffile)

        app_properties_path = "/srv/trac/common/TracPdfFill.properties"
        tmpl_properties_path = "%s/.pdf/TemplateFEE.properties" % self.path

        # Form fields rectangles coordinates
        with open(tmpl_properties_path, 'w') as f:
            f.write("# TemplateFEE properties\n")
            f.write("# sign rectangles (lower left x, lower left y) "
                    "(upper right x, upper right y)\n")
            dist_rectangles = self.get_dist_rectangles(sourcefile)
            for i, ((llXValue, llYValue),
                    (urXValue, urYValue)) in enumerate(dist_rectangles,
                                                       start=1):
                f.write("fieldrectangle%s=%s\n" % (i, llXValue.text))
                f.write("fieldrectangle%s=%s\n" % (i, llYValue.text))
                f.write("fieldrectangle%s=%s\n" % (i, urXValue.text))
                f.write("fieldrectangle%s=%s\n" % (i, urYValue.text))

        # Create form fields in the PDF
        unix_cmd_list = ['sudo /usr/bin/java '
                         '-jar /srv/trac/common/TracPdfFill.jar'
                         ' -P "%s"'
                         ' -p "%s"'
                         ' -a "%s"'
                         ' -s "%s"'
                         ' -d "%s"' % (
                             app_properties_path,
                             tmpl_properties_path,
                             "CreateFields",
                             src,
                             dest)]

        # Copy modified PDF to the working copy (if not empty)
        unix_cmd_list += ['if [[ -s "%s" ]] ; then cp -f "%s" "%s"; fi'
                          % (dest, dest, src)]

        retcode, lines = util.unix_cmd_apply(self.env, unix_cmd_list,
                                             util.lineno())
        if retcode != 0:
            raise TracError('\n'.join(lines))



class DOC_Cache(Ticket_Cache):

    def __init__(self, env, trac_env_name, authname, ticket):
        super(DOC_Cache, self).__init__(env, trac_env_name, authname, ticket)
        tagged_item = ticket['summary'].strip('DOC_')
        indexes = [tg.status_index for tg in model.Tag.select(
            self.env,
            ["tagged_item='%s'" % tagged_item, "status='Draft'"],
            ordering_term='status_index',
            tag_type='version_tags')]
        if indexes:
            index = indexes[-1] + 1
        else:
            index = 1
        self.version_status = 'Draft%s' % index
        self.template = None
        source_types = util.get_prop_values(env, "source_types")
        sourcetype = ticket['sourcetype']
        if sourcetype in source_types and source_types[sourcetype]:
            template_fn = source_types[sourcetype].split('||')[0].strip()
            if template_fn:
                for template_dir in Chrome(self.env).get_templates_dirs():
                    template = '%s/%s' % (template_dir, template_fn)
                    if os.path.isfile(template):
                        self.template = template
                        break
                else:
                    raise TracError('Unable to find DOC template')
        self.ns0 = '%s/DOC' % self.schemas_url
        self.ns0_legacy_list = ["%s/%s" % (url, 'DOC') for url in self.schemas_url_legacy_list]
        self.root_tag = "{%s}DOC" % self.ns0
        self.root_tag_legacy_list = ["{%s}%s" % (ns0, 'DOC') for ns0 in self.ns0_legacy_list]
        self.trac_data = {'Identification': {'DocumentName': self.ci_name,
                                             'VersionId': self.version_id,
                                             'VersionStatus': self.version_status,
                                             'VersionOldStatus': self.version_status}}
        equipment = self.env.config.get('artusplugin', 'equipment')
        program = self.env.config.get('artusplugin', 'program')
        if equipment and program:
            self.trac_data['Identification']['EquipmentProgram'] = '%s %s' % (equipment, program)
        submittedfor = 'submittedfor' in ticket.values and ticket['submittedfor'] or None
        if submittedfor:
            self.trac_data['Submission'] = {'SubmittedFor': submittedfor}
        

    def get_sign_rectangles(self, docfile):
        """ Return document sign rectangles coordinates
        """
        if docfile and docfile.endswith('.docm') and self.exist_in_wc(docfile):
            # Extract sign rectangles from source file
            # Compatibility with old formats
            self.unzip(docfile)
            customxml_filename = self.get_customxml_filepath()
            customxml_tree = ElementTree.parse(customxml_filename)
        elif self.template:
            # Extract sign rectangles from template
            # Compatibility with current format only
            temp_dir = mkdtemp(dir='/tmp')
            unix_cmd_list = ['unzip -d"%s" "%s"' % (
                temp_dir, self.template)]
            retcode, lines = util.unix_cmd_apply(self.env, unix_cmd_list,
                                                 util.lineno())
            if retcode != 0:
                shutil.rmtree(temp_dir)
                raise TracError('\n'.join(lines))

            customxml_filename = self.get_customxml_filepath(temp_dir)
            customxml_tree = ElementTree.parse(customxml_filename)
            shutil.rmtree(temp_dir)
        else:
            customxml_tree = None

        if customxml_tree:
            customxml_root_tag = customxml_tree.getroot().tag
            for ns0 in [self.ns0] + self.ns0_legacy_list:
                if ns0 in customxml_root_tag:
                    customxml_xvalues = customxml_tree.getiterator('{%s}XValue' % ns0)
                    customxml_yvalues = customxml_tree.getiterator('{%s}YValue' % ns0)
                    xy_list = zip(customxml_xvalues, customxml_yvalues)
                    for i in range(0, len(xy_list), 2):
                        rect = xy_list[i:i + 2]
                        yield tuple(rect)
        else:
            yield

    def update_data(self, docfile, force=False):
        """ Update document data
            (limited to word document)
        """
        if docfile and docfile.endswith('.docm') and self.exist_in_wc(docfile):
            self.unzip(docfile)
            # Prepare trac-data.xml
            self.form_data = SourceData(self.env,
                                        self.form_filename,
                                        self.schemas_url,
                                        'ns0:DOC',
                                        dict(zip(['ns0:%s' % group for group in self.trac_data.keys()],
                                                 [[['ns0:%s' % data for data in group_data.keys()]]
                                                  for group_data in self.trac_data.values()]))
                                        )

            if self.form_data.trac_data['ns0:Identification'].data[0]['ns0:VersionStatus']:
                self.trac_data['Identification']['VersionOldStatus'] = self.form_data.trac_data['ns0:Identification'].data[0]['ns0:VersionStatus']

            for group in self.trac_data.keys():
                for data in self.trac_data[group].keys():
                    self.form_data.trac_data['ns0:%s' % group].data[0]['ns0:%s' % data] = self.trac_data[group][data]

            self.form_data.write(self.form_data.toxml)
            update_needed = False

            # Namespace is updated if needed
            for line, new in util.inplace(self.get_customxml_filepath()):
                for schemas_url_legacy in self.schemas_url_legacy_list:
                    if schemas_url_legacy in line:
                        line = line.replace(schemas_url_legacy, self.schemas_url)
                        update_needed = True
                        break
                new.write(line)

            # update of document Identification only if needed
            # trac_data.xml replaces the Identification part of the custom XML
            # if one part of that Identification has changed
            # Identification must not be updated after it has been changed
            # to Released status by the user
            for group in self.trac_data.keys():
                customxml_tree = ElementTree.parse(self.get_customxml_filepath())
                if customxml_tree:
                    customxml_root = customxml_tree.getroot()
                    customxml_root_tag = customxml_root.tag
                    for ns0 in [self.ns0] + self.ns0_legacy_list:
                        if ns0 in customxml_root_tag:
                            customxml_id = customxml_tree.find('{%s}%s' % (ns0, group))
                            if customxml_id is not None and len(customxml_id):
                                for data in self.trac_data[group].keys():
                                    customxml_elt = customxml_id.find('{%s}%s' % (ns0, data))
                                    if customxml_elt is not None:
                                        customxml_text = customxml_elt.text or ''
                                        trac_data_text = self.form_data.trac_data['ns0:%s' % group].data[0]['ns0:%s' % data]
                                        if customxml_text != trac_data_text.replace('\r\n', '\n'):
                                            if data == 'VersionStatus' and force is False and customxml_text == 'Released':
                                                continue
                                            else:
                                                data_change = True
                                                break
                                else:
                                    data_change = False  
                                if data_change:
                                    customxml_root.remove(customxml_id)
                                    trac_data_tree = ElementTree.parse(self.form_filename)
                                    trac_data_id = trac_data_tree.find('{%s}%s' % (self.ns0, group))
                                    customxml_root.append(trac_data_id)
                                    customxml_tree.write(self.get_customxml_filepath())
                                    update_needed = True
                else:
                    message = tag.span('%s: Error parsing custom xml file:' % util.lineno())
                    message(tag.p(self.get_customxml_filepath()))
                    raise TracError(message, 'Hostname mismatch ?', True)

            if update_needed:
                self.zip(docfile)

    def add_attachments(self, pdffile):

        # Create sub-directory for attaching attachments to the PDF
        pdf_dir = "%s/.pdf" % self.path
        if not os.path.exists(pdf_dir):
            os.makedirs(pdf_dir)

        src = "%s/%s" % (self.path, pdffile)
        dest = '%s/%s' % (pdf_dir, pdffile)

        attachments = []
        for attachment in Attachment.select(self.env, 'ticket', self.id):
            attachment_local_path = '%s/%s' % (pdf_dir, attachment.filename)
            shutil.copy(attachment.path, attachment_local_path)
            attachments.append(attachment_local_path)

        if attachments:
            util.pdf_attach_files(src, attachments, dest)
            shutil.copy(dest, src)


cacheddocument_subclasses = {'ECM': ECM_Cache,
                             'FEE': FEE_Cache,
                             'DOC': DOC_Cache,
                             }


class SourceData(object):
    """ Data that is imported into source file """

    def __init__(self, env, filename, schemas_url, root_tag, data_tags):
        self.env = env
        self.filename = filename
        self.schemas_url = schemas_url
        self.root_tag = root_tag
        self.node_tags = data_tags.keys()
        self.data_tags = data_tags
        self.ticket_type = root_tag.strip('ns0:')
        self.ns0 = '%s/%s' % (self.schemas_url, self.ticket_type)
        try:
            self.dom = parse(self.filename)
        except Exception:
            self.dom = None
        self.trac_data = {}
        for node_tag in self.node_tags:
            self.trac_data[node_tag] = util.DataSet(self.env,
                                                    self.dom,
                                                    self.root_tag,
                                                    False,
                                                    self.data_tags[node_tag])

    def write(self, callback):
        f = codecs.open(self.filename, 'w', 'utf-8')
        f.write(callback())
        f.close()

    def toxml(self):
        xml_string = ('<%s xmlns:ns0="%s"'
                      ' xmlns:ns1="http://www.w3.org/2001/XMLSchema-instance"'
                      ' ns1:noNamespaceSchemaLocation="%s/%s/schema%s.xsd">')
        xml_string %= (self.root_tag,
                       self.ns0,
                       self.schemas_url,
                       self.ticket_type,
                       self.ticket_type)
        for node_tag in self.node_tags:
            xml_string += u'\n  <%s>' % node_tag
            tags = self.data_tags[node_tag] if isinstance(self.data_tags[node_tag], list) else [self.data_tags[node_tag]]
            for index, item_tags in enumerate(tags):
                if 'Configuration' in node_tag:
                    xml_string += u'\n    <ns0:Item>'
                for tag in item_tags:
                    xml_string += u'\n      <%s>%s</%s>' % (
                        tag,
                        (self.trac_data[node_tag].data[index][tag] and
                        cgi.escape(self.trac_data[node_tag].data[index][tag]) or
                        self.trac_data[node_tag].data[index][tag]),
                        tag)
                if 'Configuration' in node_tag:
                    xml_string += u'\n    </ns0:Item>'
            xml_string += u'\n  </%s>' % node_tag
        xml_string += '\n</%s>' % self.root_tag
        return xml_string


class PDFPackage(object):
    """ Extracts documents from the repository and
        packages them into one or more archives """

    @staticmethod
    def get_src_files(env, doc_url, select=True, sourcefile=None):
        """
        Scans a document folder and returns the source documents found.

        Amongst those documents, only ONE may be selected. A source file
        is selected if it fullfills the following requirements:

            REQ1:   The source file has no matching PDF file or
                    it has only one matching PDF file and the PDF file
                    revision is greater or equal to the source file revision

                    A source file is defined as:
                      * an OpenOffice or LibreOffice document
                        (odt,ods) or
                        a Microsoft Office document
                        (docx,docm,doc,rtf,xlsx,xlsm,xls) or

                    A PDF file is defined as:
                      * a file with .pdf suffix

                    A matching PDF file is defined as:
                      * a PDF file with the same name as the source file
                        when the extension is stripped

            REQ2:   In case several source files comply with REQ1, the source
                    file has the same name as the document folder name

        """
        if doc_url:
            log_level = env.config.get('logging', 'log_level')
            repos = util.get_repository(env, doc_url)
            match = re.search(r'(?:.+)?(/(?:trunk|tags|branches)/.+)', doc_url)
            repo_url = util.get_url(match.group(1))
            repo_rev = util.get_revision(doc_url)
            # there is a race condition with post-commit hook
            for i in range(1, 5):  # @UnusedVariable
                try:
                    node = repos.get_node(repo_url, repo_rev)
                    break
                except Exception:
                    time.sleep(1)
                    continue
            else:
                return []

            def get_real_rev(node):
                for h in node.get_history(10):
                    if h[2] == 'copy':
                        continue
                    rev = h[1]
                    break
                return rev

            files = [(entry.name, get_real_rev(entry))
                     for entry in node.get_entries() if entry.isfile]
            pdf_files = [(name, rev) for (name, rev) in files
                         if name.lower().endswith('.pdf')]
            suffx = util.get_prop_values(env, 'source_files_suffix')
            src_files = [(name, rev) for (name, rev) in files
                         if name.split('.')[-1] in suffx]
            src_selected = []
            if select:
                if len(src_files) == 0:
                    if log_level == 'INFO':
                        syslog.syslog("INFO for doc url %s: No source found" % doc_url)
                else:
                    if sourcefile and sourcefile in dict(src_files):
                        # Forced selection
                        src_selected.append((sourcefile, True))
                    else:
                        for src_file in src_files:
                            pdfs = [(name, rev) for (name, rev) in pdf_files
                                    if name.rsplit('.', 1)[0] ==
                                    src_file[0].rsplit('.', 1)[0]]
                            if (len(pdfs) == 0 or
                                (len(pdfs) == 1 and
                                 src_file[1] <= pdfs[0][1])):
                                # Temporary selection
                                src_selected.append(src_file)
                    if len(src_selected) == 0:
                        if log_level == 'INFO':
                            syslog.syslog("INFO for doc url %s: "
                                          "No source comply with REQ1" % doc_url)
                    else:
                        if len(src_selected) > 1:
                            # Definitive selection
                            src_selected = [src for src in src_selected
                                            if src[0].rsplit('.', 1)[0] ==
                                            repo_url.rsplit('/', 1)[-1]]
                            if len(src_selected) > 1:
                                # The greater revision only is selected
                                src_selected = [max([(value, key)
                                                     for key, value in
                                                     dict(src_selected).items()])[::-1]]
                        if len(src_selected) == 0:
                            if log_level == 'INFO':
                                syslog.syslog("INFO for doc url %s: "
                                              "No source comply with REQ2" % doc_url)
                return [(src[0], True) for src in src_selected] + \
                       [(src[0], False) for src in src_files
                        if src[0] not in dict(src_selected).keys()]
            else:
                return [(src[0], False) for src in src_files]
        else:
            return []

    @staticmethod
    def get_pdf_files(env, doc_url, select=True, sourcefile=None, sourcerev=None):
        """
        Scans a document folder and returns the PDF documents found.

        Amongst those documents, only ONE may be selected. A PDF file
        is selected if it fullfills the following requirements:

            REQ1:   The PDF file has no matching source file or
                    it has only one matching source file and the PDF file
                    revision is greater or equal to the source file revision

                    A pdf file is defined as:
                      * a file with .pdf suffix

                    A source file is defined as:
                      * an OpenOffice or LibreOffice document
                        (odt,ods) or
                        a Microsoft Office document
                        (docx,docm,doc,rtf,xlsx,xlsm,xls) or

                    A matching source file is defined as:
                      * a source file with the same name as the PDF file
                        when the extension is stripped

            REQ2:   In case several PDF files comply with REQ1, the PDF file
                    has the same name as the document folder name

        """
        if doc_url:
            log_level = env.config.get('logging', 'log_level')
            repos = util.get_repository(env, doc_url)
            match = re.search(r'(?:.+)?(/(?:trunk|tags|branches)/.+)', doc_url)
            repo_url = util.get_url(match.group(1))
            repo_rev = util.get_revision(doc_url)
            # there is a race condition with post-commit hook
            for i in range(1, 5):  # @UnusedVariable
                try:
                    node = repos.get_node(repo_url, repo_rev)
                    break
                except Exception:
                    time.sleep(1)
                    continue
            else:
                return []

            def get_real_rev(node):
                for h in node.get_history(10):
                    if h[2] == 'copy':
                        continue
                    rev = h[1]
                    break
                return rev

            files = [(entry.name, get_real_rev(entry))
                     for entry in node.get_entries() if entry.isfile]
            pdf_files = [(name, rev) for (name, rev) in files
                         if name.lower().endswith('.pdf')]
            suffx = util.get_prop_values(env, 'source_files_suffix')
            src_files = [(name, rev) for (name, rev) in files
                         if name.split('.')[-1] in suffx]
            pdf_selected = []
            if select:
                if len(pdf_files) == 0:
                    if log_level == 'INFO':
                        syslog.syslog("INFO for doc url %s: No PDF found" % doc_url)
                else:
                    for pdf_file in pdf_files:
                        sources = [(name, rev) for (name, rev) in src_files
                                   if name.rsplit('.', 1)[0] ==
                                   pdf_file[0].rsplit('.', 1)[0]]
                        if sourcefile and sourcefile in dict(sources):
                            if sourcerev:
                                if pdf_file[1] >= sourcerev:
                                    # Temporary selection
                                    pdf_selected.append(pdf_file)
                            else:
                                if pdf_file[1] >= dict(sources)[sourcefile]:
                                    # Temporary selection
                                    pdf_selected.append(pdf_file)
                        else:
                            if (len(sources) == 0 or
                                (len(sources) == 1 and
                                 pdf_file[1] >= sources[0][1])):
                                # Temporary selection
                                pdf_selected.append(pdf_file)
                    if len(pdf_selected) == 0:
                        if log_level == 'INFO':
                            syslog.syslog("INFO for doc url %s: "
                                          "No PDF comply with REQ1" % doc_url)
                    else:
                        if len(pdf_selected) > 1:
                            # Definitive selection
                            programidre = env.config.get('artusplugin', 'programidre')
                            skill_options = env.config.get('ticket-custom', 'skill.options')
                            m = re.search(model.NamingRule.get_ci_name_pattern(
                                programidre,
                                skill_options,
                                repo_url.startswith('/tags/'),
                                'document',
                                'ER'), repo_url, re.UNICODE)
                            if m:
                                ci_name = m.group('ci_name')
                                pdf_selected = [pdf for pdf in pdf_selected
                                                if pdf[0].rsplit('.', 1)[0] ==
                                                ci_name]
                            if len(pdf_selected) > 1:
                                # The greater revision only is selected
                                pdf_selected = [max([(value, key)
                                                     for key, value in
                                                     dict(pdf_selected).items()])[::-1]]
                        if len(pdf_selected) == 0:
                            if log_level == 'INFO':
                                syslog.syslog("INFO for doc url %s: "
                                              "No PDF comply with REQ2" % doc_url)

                return [(pdf[0], True) for pdf in pdf_selected] + \
                       [(pdf[0], False) for pdf in pdf_files
                        if pdf[0] not in dict(pdf_selected).keys()]
            else:
                return [(pdf[0], False) for pdf in pdf_files]
        else:
            return []

    @staticmethod
    def get_archives_number(ticket):
        program_data = util.get_program_data(ticket.env)
        trac_env_name = program_data['trac_env_name']
        base_dir = '/var/cache/trac/PDF-packaging/%s' % trac_env_name
        # Creates base_dir and intermediate directories if they don't exist
        if not os.access(base_dir, os.F_OK):
            os.makedirs(base_dir)
        archives_number = len([f for f in os.listdir(base_dir) if re.search(r'^%s(\.\d+)?\.zip$' % ticket['summary'], f)])

        return archives_number

    @staticmethod
    def get_archives_paths(ticket):
        program_data = util.get_program_data(ticket.env)
        trac_env_name = program_data['trac_env_name']
        base_dir = '/var/cache/trac/PDF-packaging/%s' % trac_env_name
        # Creates base_dir and intermediate directories if they don't exist
        if not os.access(base_dir, os.F_OK):
            os.makedirs(base_dir)
        archives_names = [f for f in os.listdir(base_dir) if re.search(r'^%s(\.\d+)?\.zip$' % ticket['summary'], f)]
        archives_paths = ['%s/%s' % (base_dir, a) for a in archives_names]

        return archives_paths

    @staticmethod
    def get_archives_content(ticket):
        program_data = util.get_program_data(ticket.env)
        trac_env_name = program_data['trac_env_name']
        base_dir = '/var/cache/trac/PDF-packaging/%s' % trac_env_name
        # Creates base_dir and intermediate directories if they don't exist
        if not os.access(base_dir, os.F_OK):
            os.makedirs(base_dir)
        idx_path = '%s/%s.idx' % (base_dir, ticket['summary'])
        archives_content = OrderedDict()
        try:
            with open(idx_path) as f:
                for element in [line.strip(' \n').split(' ') for line in f.readlines()]:
                    archives_content.setdefault(element[0], []).append(element[1])
            for no in archives_content.keys():
                archive_documents = OrderedDict()
                for archive_document in [filepath.split('/', 1) for filepath in archives_content[no]]:
                    archive_documents.setdefault(archive_document[0], []).append(archive_document[1])
                archives_content[no] = archive_documents
        except IOError:
            # Archive index not found
            pass

        return archives_content

    @staticmethod
    def get_archives_documents(archives_content):
        # For a given tgname, directory/files may be spread over several archives
        archives_documents = OrderedDict()
        for no in archives_content.keys():
            for tgname in archives_content[no]:
                archives_documents.setdefault(tgname, []).extend(archives_content[no][tgname])

        return archives_documents

    @staticmethod
    def get_archives_documents_paths(archives_documents, selected_documents):
        archives_documents_paths = OrderedDict()
        for tgname in archives_documents.keys():
            selected_fn = list(selected_documents[tgname])  # makes a copy
            for counter, path in enumerate(archives_documents[tgname]):
                ext = path.split('.')[-1]
                if ext.lower() == 'pdf':
                    fn = path.split('/')[-1]
                    if fn in selected_fn:
                        # Not renamed
                        archives_documents_paths.setdefault(tgname, []).append(path)
                        selected_fn.remove(fn)
                    else:
                        # renamed
                        index = counter
                else:
                    archives_documents_paths.setdefault(tgname, []).append(path)
            if selected_fn:
                archives_documents_paths.setdefault(tgname, []).insert(index, selected_fn[0])

        return archives_documents_paths

    def __init__(self, env, authname, pdf_list,
                 pdf_rename, max_size, prf_chklst, source_files, package_name=None):
        self.env = env
        program_data = util.get_program_data(self.env)
        self.trac_env_name = program_data['trac_env_name']
        self.program_name = program_data['program_name']
        self.authname = authname
        if package_name:
            self.package_name = package_name
        else:
            self.package_name = '%s_%s_%s' % (
                self.trac_env_name,
                unicode(datetime.now(localtz).strftime('%Y-%m-%d_%H-%M-%S')),
                self.authname)
        self.pdf_list = pdf_list
        # Case where only one element is selected
        if not type(self.pdf_list) == list:
            self.pdf_list = [pdf_list]
        self.pdf_rename = pdf_rename
        self.prf_chklst = prf_chklst
        self.source_files = source_files
        self.max_size = max_size
        self.base_dir = '/var/cache/trac/PDF-packaging/%s' % self.trac_env_name
        self.pdf_tag_files = []
        for pdf in self.pdf_list:
            self.pdf_tag_files.append(pdf.split('/'))

    def build(self):
        # Creates base_dir and intermediate directories if they don't exist
        if not os.access(self.base_dir, os.F_OK):
            os.makedirs(self.base_dir)

        # Init
        self.base_path = '%s/%s' % (self.base_dir, self.package_name)
        self.flags = os.O_CREAT + os.O_WRONLY + os.O_EXCL
        if hasattr(os, 'O_BINARY'):
            self.flags += os.O_BINARY
        self.build_result = 'success'
        self.build_message = ''
        self.archive_name = 'archive.zip'
        self.archive_path = '%s/%s' % (self.base_path, self.archive_name)
        self.zip_dirpath = "%s/zip_content" % self.base_path
        self.zipsplits = []
        self.ticket_types = [t.name for t in Type.select(self.env)]
        self.pdf_selected = {}

        # Cleaning
        for f in os.listdir(self.base_dir):
            if re.search('%s.*?\.(zip|idx)' % self.package_name, f):
                os.remove(os.path.join(self.base_dir, f))
        if os.access(self.base_path, os.F_OK):
            shutil.rmtree(self.base_path)
        os.mkdir(self.base_path)
        os.mkdir(self.zip_dirpath)

        # Setup of zip content
        for pdf_tag_file in self.pdf_tag_files:
            if pdf_tag_file[0] not in self.pdf_selected:
                tg = model.Tag(self.env, name=pdf_tag_file[0])
                pdf_data = PDFPackage.get_pdf_files(self.env, tg.tag_url)
                tkid = util.get_doc_tktid(
                    self.env, tg.tagged_item)
                if tkid:
                    ticket = Ticket(self.env, tkid)
                    pdffile = ticket['pdffile']
                    if pdffile and pdffile != 'N/A':
                        for counter, item in enumerate(pdf_data):
                            if item[0] == pdffile:
                                pdf_data[counter] = (pdffile, True)
                                break
                self.pdf_selected[tg.name] = dict(pdf_data)
            # Rename IF main PDF file
            if self.pdf_rename and self.pdf_selected[tg.name][pdf_tag_file[1]]:
                targetfilename = "%s.pdf" % pdf_tag_file[0]
                # For external documents
                targetfilename = targetfilename.replace(
                    "%s_EXT_" % self.program_name, "", 1)
            else:
                targetfilename = pdf_tag_file[1]
            doc_dirpath = "%s/%s" % (self.zip_dirpath, tg.name)
            if not os.access(doc_dirpath, os.F_OK):
                os.mkdir(doc_dirpath)
            repos = util.get_repository(self.env, tg.tag_url)
            try:
                if repos.reponame:
                    node_url = tg.tag_url[len(repos.reponame) + 1:]
                else:
                    node_url = tg.tag_url
                node = repos.get_node('%s/%s' % (util.get_url(node_url),
                                                 pdf_tag_file[1]),
                                      util.get_revision(node_url))
                doc_filepath = '%s/%s' % (doc_dirpath, targetfilename)
                targetfile = os.fdopen(os.open(doc_filepath, self.flags, 0666), 'w')
                shutil.copyfileobj(node.get_content(), targetfile)
                targetfile.close()
                # Add associated (P)RF/CHKLST IF main PDF file
                if self.prf_chklst and self.pdf_selected[tg.name][pdf_tag_file[1]]:
                    self._attach_prf_chklst(tg, self.base_path, doc_dirpath, targetfilename)
                # Add associated source files IF main PDF file
                if self.source_files and self.pdf_selected[tg.name][pdf_tag_file[1]]:
                    retcode, lines = self._export_source_files(tg, doc_dirpath)
                    if retcode != 0:
                        self.build_result = 'failure'
                        self.build_message = ' '.join(lines)
                        return
            except NoSuchNode:
                pass  # ignore broken repositories used for testing
            except Exception:
                exc_info = sys.exc_info()
                exc_obj = exc_info[1]
                exc_tb = exc_info[2]
                fname = exc_tb.tb_frame.f_code.co_filename
                self.build_result = 'failure'
                self.build_message = '%s:%s %s' % (fname, exc_tb.tb_lineno, exc_obj)
                return

        # An archive is made
        cmd = 'cd "%s";find . | sort | /usr/bin/zip "%s" -@' % (self.zip_dirpath, self.archive_path)
        unix_cmd_list = [cmd]
        retcode, lines = util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())
        if retcode != 0:
            self.build_result = 'failure'
            self.build_message = ' '.join(lines)
            return

        self.how_many = None
        if os.access(self.archive_path, os.F_OK):
            if self.max_size == 'No split':
                max_size = '2000000000'
            else:
                max_size = '%s000000' % self.max_size[:-1]

            # Split archive
            cmd = '/usr/bin/zipsplit -i -s -n %s -b "%s" "%s"' % (
                max_size, self.base_path, self.archive_path)
            retcode, lines = util.unix_cmd_apply(self.env, [cmd], util.lineno())

            if retcode != 0:
                self.build_result = 'failure'
                self.build_message = ' '.join(lines)
                return
            else:
                # Each splitted archive is made available
                # Naming rule of each split is not well documented
                # It seems it is limited to 8 characters + '.zip'
                # So splits are NOT determined through a template
                for f in os.listdir(self.base_path):
                    if os.path.splitext(f)[1] == '.zip' and f != self.archive_name:
                        self.zipsplits.append(f)
                self.how_many = len(self.zipsplits)

                for idx in range(1, self.how_many + 1):
                    original_path = '%s/%s' % (self.base_path, self.zipsplits[idx - 1])
                    final_path = '%s/%s.%d.zip' % (
                        self.base_dir, self.package_name, idx)
                    if os.access(original_path, os.F_OK):
                        os.rename(original_path, final_path)
                if self.how_many == 1:
                    original_path = final_path
                    final_path = '%s/%s.zip' % (
                        self.base_dir, self.package_name)
                    if os.access(original_path, os.F_OK):
                        os.rename(original_path, final_path)

                # The index file is made available
                original_path = '%s/zipsplit.idx' % self.base_path
                final_path = '%s/%s.idx' % (
                    self.base_dir, self.package_name)
                if os.access(original_path, os.F_OK):
                    os.rename(original_path, final_path)
        else:
            self.build_result = 'failure'
            self.build_message = 'Could not find built archive'
            return

    def _attach_prf_chklst(self, tg, base_path, doc_dirpath, targetfilename):
        db = self.env.get_db_cnx()
        cursor = db.cursor()
        tkt_ids = []
        for rf_type in ('RF', 'PRF'):
            if rf_type in self.ticket_types:
                cursor.execute("SELECT id FROM ticket "
                               "WHERE summary LIKE "
                               "'%s\_%s\_%%' ESCAPE '\\'" % (
                                   rf_type, tg.name))
                tkt_ids += [int(row[0]) for row in cursor]
                if not tkt_ids and tg.status == 'Released':
                    cursor.execute("SELECT status_index FROM tag "
                                   "WHERE tagged_item='%s' "
                                   "AND status='Proposed' "
                                   "ORDER BY status_index DESC" % tg.tagged_item)
                    row = cursor.fetchone()
                    if not row:
                        # No PRF has been done for releasing !
                        continue
                    else:
                        status_index = row[0]
                    cursor.execute("SELECT id FROM ticket "
                                   "WHERE summary LIKE "
                                   "'%s\_%s.Proposed%s\_%%' ESCAPE '\\'" % (
                                       rf_type, tg.tagged_item, status_index))
                    tkt_ids += [int(row[0]) for row in cursor]

        attachments = []
        for tkt_id in tkt_ids:
            ticket = Ticket(self.env, tkt_id)
            tp_data = TicketForm.get_ticket_process_data(self.env, self.authname, ticket)
            tf = tp_data['ticket_form']
            pdf_path = tf.pdf_convert(ticket, base_path)
            if pdf_path:
                # Include P(RF) into the PDF attachments list
                attachments.append(pdf_path)

        if attachments:
            util.pdf_attach_files("%s/%s" % (doc_dirpath, targetfilename),
                                  attachments,
                                  "%s/_%s" % (doc_dirpath, targetfilename))

            if os.access('%s/_%s' % (doc_dirpath, targetfilename), os.F_OK):
                os.rename('%s/_%s' % (doc_dirpath, targetfilename),
                          '%s/%s' % (doc_dirpath, targetfilename))

    def _export_source_files(self, tg, target_dir):
        repo_host = util.get_hostname(self.env)
        reponame = util.get_repository(self.env, tg.tag_url).reponame
        if not reponame:
            reponame = self.trac_env_name
        repo_url = "/%s%s/" % (reponame, util.get_url(util.repo_path(tg.tag_url)))
        if not target_dir.endswith('/'):
            target_dir += '/'
        suffx = util.get_prop_values(self.env, 'source_files_suffix')
        regexp = "(%s)" % ('|'.join(suffx.keys()))

        unix_cmd_list = ['/srv/svn/common/svn_export_with_filter.sh "%s" "%s" "%s" "%s"' %
                         (repo_host,
                          repo_url,
                          target_dir,
                          regexp)]
        # Effective application of the list of commands
        return(util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno()))

    def _format_img(self, data):
        data_img = urllib2.urlopen('%s/htdocs/%s' % (data['host_url'], data['image'])).read()
        return data_img

    def _format_html(self, data):
        chrome = Chrome(self.env)
        dirs = []
        for provider in chrome.template_providers:
            dirs += provider.get_templates_dirs()
        templates = TemplateLoader(dirs, variable_lookup='lenient')

        _buffer = StringIO()
        try:
            template = templates.load('pdf_packaging_job.html', cls=MarkupTemplate)
            if template:
                stream = template.generate(**data)
                stream.render('xhtml', doctype=DocType.XHTML_STRICT, out=_buffer)
        except TemplateNotFound:
            pass

        return _buffer.getvalue()

    def _format_text(self, data):
        if data['build_result'] == 'success':
            if data['how_many'] > 1:
                text = (u"You have submitted a job of PDF documents packaging. The job has succeeded.\n\n"
                        u"You have requested a maximum size of %s for the generated archives.\n\n"
                        u"Here are the links to download the zip files containing the PDF files:\n\n"
                        u"%s\n\n"
                        u"Here is the link to download the associated index file:\n\n"
                        u"%s\n\n"
                        u"These links will remain valid at least for 10 days. After that, the files may have been removed.\n\n"
                        u"You may reply to this mail if something goes wrong.\n\n"
                        u"TRAC Admin") % (data['max_size'], '\n'.join(data['links']), data['index'])
            else:
                text = (u"You have submitted a job of PDF documents packaging. The job has succeeded.\n\n"
                        u"Here is the link to download the zip file containing the PDF files:\n\n"
                        u"%s\n\n"
                        u"This link will remain valid at least for 10 days. After that, the file may have been removed.\n\n"
                        u"You may reply to this mail if something goes wrong.\n\n"
                        u"TRAC Admin") % data['link']
        else:
            text = (u"You have submitted a job of PDF documents packaging. The job has failed.\n\n"
                    u"Here is the error message(s):\n\n"
                    u"%s\n\n"
                    u"Please provide more details if possible by replying to this mail.\n\n"
                    u"TRAC Admin") % data['build_message']

        return text

    def notify(self, compmgr):
        """Send a MIMEMultipart message."""
        hostname = util.get_hostname(self.env)
        host = hostname.split('.')[0]
        scheme = self.env.config.get('artusplugin', 'scheme')
        host_url = '%s://%s' % (scheme, hostname)
        project_name = self.env.project_name
        project_desc = self.env.project_description
        smtp_from_name = self.env.config.get('announcer', 'smtp_from_name')
        smtp_from = self.env.config.get('announcer', 'smtp_from')
        (head, tail) = os.path.split(self.base_path)
        users_ldap_names = util.Users(self.env).users_ldap_names
        path_tmpl = "%s://%s/tracs/%s/PDF-packaging/%s" % (
            scheme, util.get_hostname(self.env), os.path.basename(head), tail)
        data = dict(
            scheme=scheme,
            host_url=host_url,
            path_tmpl=path_tmpl,
            build_result=self.build_result,
            project_name = project_name,
            project_desc = project_desc,
            host = host
        )
        if self.build_result == 'success':
            data['how_many'] = self.how_many
            if self.how_many > 1:
                data['max_size'] = self.max_size
                data['links'] = []
                for idx in range(1, self.how_many + 1):
                    data['links'].append('%s.%d.zip' % (path_tmpl, idx))
                data['index'] = '%s.idx' % path_tmpl
            else:
                data['link'] = '%s.zip' % path_tmpl
        else:
            data['build_message'] = self.build_message

        msg = MIMEMultipart()

        # email body
        html = self._format_html(data)
        if html:
            part = MIMEText(html, 'html', 'utf-8')
        else:
            text = self._format_text(data)
            part = MIMEText(text, 'plain', 'utf-8')
        msg.attach(part)

        trac_version = get_pkginfo(trac.core).get('version', trac.__version__)
        artusplugin_version = get_pkginfo(artusplugin).get('version', 'Undefined')

        msg['X-Mailer'] = 'ArtusPlugin v%s on Trac v%s' % (
            artusplugin_version,
            trac_version)
        msg['X-Trac-Version'] = trac_version
        msg['X-ArtusPlugin-Version'] = artusplugin_version
        msg['X-Trac-Project'] = self.env.project_name
        msg['Precedence'] = 'bulk'
        msg['Auto-Submitted'] = 'auto-generated'
        msg['Accept-Language'] = 'en-GB'
        msg['Content-Language'] = 'en-GB'

        def get_address(username):
            with Ldap_Utilities() as ldap_util:
                email = util.Users.get_email(self.env, username, ldap_util)
            displayname = users_ldap_names[username]
            displayname = unicode(displayname, "utf-8")
            displayname = '"%s"' % unidecode(displayname)
            return (displayname, email)

        from_user = '%s <%s>' % get_address(self.authname)
        to_user = from_user
        msg['Subject'] = "TRAC PDF packaging job: %s" % tail
        msg['From'] = from_user
        msg['Sender'] = '"%s" <%s>' % (smtp_from_name, smtp_from)
        msg['To'] = to_user
        admin = self.env.project_admin.split('@')[0]
        msg['Reply-To'] = '%s <%s>' % get_address(admin)

        s = smtplib.SMTP('localhost')
        s.sendmail(from_user, [to_user], msg.as_string())
        s.quit
