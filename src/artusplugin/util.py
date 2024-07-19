# -*- coding: utf-8 -*-
#
# Copyright (C) 2016 Artus
# All rights reserved.
#
# Author: Michel Guillot <michel.guillot@meggitt.com>

""" Utility functions """

# Genshi
from genshi.builder import Element
from genshi.output import DocType
from genshi.template import TemplateLoader, MarkupTemplate
from genshi.template.loader import TemplateNotFound

# Trac
from trac.config import Option
from trac.core import Component, implements, TracError
from trac.perm import PermissionSystem
from trac.resource import ResourceNotFound
from trac.ticket import Ticket
from trac.util import get_pkginfo
from trac.util.text import unicode_quote_plus
from trac.versioncontrol.api import RepositoryManager, Node, Changeset, NoSuchNode
from trac.web.chrome import add_ctxtnav, Chrome

# Standard lib
from collections import OrderedDict
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from ldap_utilities import Ldap_Utilities
from unidecode import unidecode
from time import sleep
from threading import current_thread
from threading import BoundedSemaphore
from urllib import unquote_plus
import cgi
import codecs
import commands
import ConfigParser
import inspect
import os
import pyodbc
import re
import shutil
import smtplib
import sqlite3
import subprocess
import sys
import syslog
import tempfile
import unicodedata
import urllib2
import zipfile

# 3rd party modules
import posix_ipc

# Announcer Plugin
from announcerplugin.api import IAnnouncementAddressResolver
from announcerplugin.resolvers.specified import SpecifiedEmailResolver

# Same package
import artusplugin
from artusplugin import _
import trac

# Other plugin
import childtickets

# Authentication data
import ARTUS_sqlserver_data

# Compatibility Trac 0.11 => 0.12
try:
    from trac.web.api import parse_query_string  # @UnresolvedImport
except ImportError:
    from trac.web.api import arg_list_to_args, parse_arg_list

    def parse_query_string(query_string):
        return arg_list_to_args(parse_arg_list(query_string))
import StringIO
try:
    from cStringIO import StringIO as cStringIO
except ImportError:
    cStringIO = StringIO

# OrderedSet
import collections

# Constants
SVN_TEMPLATE_CMD = 'svn %(subcommand)s --non-interactive --username trac '
SVNMUCC_TEMPLATE_CMD = 'svnmucc -u trac '
apache_user = commands.getoutput("grep -Po '\AUser\s+\K.+' /etc/httpd/conf/httpd.conf")
apache_homedir = os.path.expanduser('~%s' % apache_user)


class OrderedSet(collections.MutableSet):

    def __init__(self, iterable=None):
        self.end = end = []
        end += [None, end, end]         # sentinel node for doubly linked list
        self.map = {}                   # key --> [key, prev, next]
        if iterable is not None:
            self |= iterable

    def __len__(self):
        return len(self.map)

    def __contains__(self, key):
        return key in self.map

    def add(self, key):
        if key not in self.map:
            end = self.end
            curr = end[1]
            curr[2] = end[1] = self.map[key] = [key, curr, end]

    def discard(self, key):
        if key in self.map:
            key, prev_elt, next_elt = self.map.pop(key)
            prev_elt[2] = next_elt
            next_elt[1] = prev_elt

    def __iter__(self):
        end = self.end
        curr = end[2]
        while curr is not end:
            yield curr[0]
            curr = curr[2]

    def __reversed__(self):
        end = self.end
        curr = end[1]
        while curr is not end:
            yield curr[0]
            curr = curr[1]

    def pop(self, last=True):
        if not self:
            raise KeyError('set is empty')
        key = self.end[1][0] if last else self.end[2][0]
        self.discard(key)
        return key

    def __repr__(self):
        if not self:
            return '%s()' % (self.__class__.__name__,)
        return '%s(%r)' % (self.__class__.__name__, list(self))

    def __eq__(self, other):
        if isinstance(other, OrderedSet):
            return len(self) == len(other) and list(self) == list(other)
        return set(self) == set(other)


class DataSet(object):
    """ trac_data.xml """

    def __init__(self, env, dom, root_tag, handle_ts, data_tags):
        self.env = env
        if dom and dom.getElementsByTagName(root_tag):
            self.root_elt = dom.getElementsByTagName(root_tag)[0]
        else:
            self.root_elt = None
        self.root_tag = root_tag
        self.handle_ts = handle_ts
        self.data_tags = data_tags
        self.data = [] if all(isinstance(elem, list) for elem in data_tags) else {}
        if isinstance(self.data, list):
            self.data.extend([{tag: self._getValue(tag) for tag in item} for item in data_tags])
        else:
            if self.handle_ts:
                self.data['CreationDateTime'] = self._getAttribute('Template', 'CreationDateTime')
                self.data['ModificationDateTime'] = self._getAttribute('Template', 'ModificationDateTime')
            for tag in self.data_tags:
                self.data[tag] = self._getValue(tag)

    def _getAttribute(self, tag, attribute):
        if self.root_elt:
            nodelist = self.root_elt.getElementsByTagName(tag)
            if nodelist:
                value = nodelist[0].getAttribute(attribute)
            else:
                value = ''
        else:
            value = ''
        return value

    def _getValue(self, tag):
        if self.root_elt:
            nodelist = self.root_elt.getElementsByTagName(tag)
            if nodelist and nodelist[0].childNodes:
                value = nodelist[0].childNodes[0].data
            else:
                value = ''
        else:
            value = ''
        return value

    def toxml(self, namespace=False, schemas_url=None):
        if namespace:
            xml_string = ('<%s xmlns="%s/%s"'
                          ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
                          ' xsi:noNamespaceSchemaLocation="%s/%s/schema%s.xsd">')
            xml_string %= (self.root_tag,
                           schemas_url,
                           self.root_tag,
                           schemas_url,
                           self.root_tag,
                           self.root_tag)
        else:
            xml_string = '<%s>' % self.root_tag
        if self.handle_ts:
            if self.data['TemplateRef']:
                xml_string += u'<Template CreationDateTime="%s" ModificationDateTime="%s"><TemplateRef>%s</TemplateRef></Template>' % (
                    self.data['CreationDateTime'] or '', self.data['ModificationDateTime'] or '', self.data['TemplateRef'])
            else:
                xml_string += u'<Template CreationDateTime="%s" ModificationDateTime="%s" />' % (
                    self.data['CreationDateTime'] or '', self.data['ModificationDateTime'] or '')
        for tag in self.data_tags:
            xml_string += u'<%s>%s</%s>' % (
                tag,
                self.data[tag] and cgi.escape(self.data[tag]) or self.data[tag],
                tag)
        xml_string += '</%s>' % self.root_tag
        return xml_string


def path_to_linux(path):
    return path.replace('\\', '/').strip('/')


def path_to_windows(path):
    return path.replace('/', '\\').strip('\\')


def my_type(my_object):
    return type(my_object)


def zip_dir(path_dir, path_file_zip):
    """ shutil.make_archive() is not thread-safe because it calls os.getcwd()
        and os.chdir which are global to the containing process. """
    with zipfile.ZipFile(path_file_zip, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for root, dirs, files in os.walk(path_dir):
            for file_or_dir in files + dirs:
                zip_file.write(
                    os.path.join(root, file_or_dir),
                    os.path.relpath(os.path.join(root, file_or_dir), path_dir))


def upload_filename(upload, suffix):
    if not hasattr(upload, 'filename') or not upload.filename:
        raise TracError(_('No file uploaded'))
    if upload.filename.split('.')[-1].lower() != suffix:
        raise TracError(_('Reqtify project expected (*.%s)' % suffix))
    if hasattr(upload.file, 'fileno'):
        size = os.fstat(upload.file.fileno())[6]
    else:
        upload.file.seek(0, 2)  # seek to end of file
        size = upload.file.tell()
        upload.file.seek(0)
    if size == 0:
        raise TracError(_("Can't upload empty file"))
    # We try to normalize the filename to unicode NFC if we can.
    # Files uploaded from OS X might be in NFD.
    filename = unicodedata.normalize('NFC', str(upload.filename))
    filename = os.path.basename(path_to_linux(filename))
    return filename


def create_archive_file(env, filelist, zipfile):
    if os.access(zipfile, os.F_OK):
        os.remove(zipfile)
    cmd = '/usr/bin/zip -j "%s" "%s"' % (zipfile, ' '.join(filelist))
    unix_cmd_list = [cmd]
    unix_cmd_apply(env, unix_cmd_list, lineno())


def dump_string(my_string):
    msg_filename = '/tmp/%s.mystring' % os.getpid()
    f = codecs.open(msg_filename, 'w', 'utf-8')
    f.write(my_string)
    f.close()


def formatted_name(authname):
    parts = authname.split('.')
    if len(parts) == 2:
        result = '%s %s' % (parts[0].capitalize(), parts[1].upper())
    else:
        result = authname

    return result

def unicode_unquote_plus(value):
    """A unicode aware version of `urllib.unquote_plus`.
    """
    str_value = str(value) if not isinstance(value, str) else value
    
    return unquote_plus(str_value).decode('utf-8')

def get_program_data(env):
    """ eg:
    base_path = /tracs/E05058SB
    trac_env_name = E05058SB
    program_name = E05058
    """
    data = {}
    data['base_path'] = env.base_url
    index = data['base_path'].rfind('/')
    data['trac_env_name'] = data['base_path'][index + 1:]
    if (data['trac_env_name'] != 'SB' and data['trac_env_name'].endswith('SB')) or data['trac_env_name'].endswith('FF'):
        data['program_name'] = data['trac_env_name'][:-2]
    else:
        data['program_name'] = data['trac_env_name']
    return data


def get_hostname(env):
    """ Get hostname."""
    scheme = env.config.get('artusplugin', 'scheme')
    host_start_index = len('%s://' % scheme)
    host_end_index = env.base_url.find('/', host_start_index)
    hostname = env.base_url[host_start_index:host_end_index]
    return hostname


def get_repo_url(env, url):
    # Get complete Subversion url from internal path
    base_url = env.base_url
    base_url = base_url.replace('/tracs', '', 1)
    repository = get_repository(env, url)
    if repository and repository.reponame:
        base_url = base_url[:base_url.rfind('/')]
    repo_url = base_url + url
    return repo_url


def get_repo_local_url(env, url):
    # Get local Subversion url from internal path
    repository = get_repository(env, url)
    repo_url = 'file://%s%s' % (repository.params['dir'], url)
    return repo_url


def get_repo_name(env, skill):
    repos = {}
    for elt in env.config['artusplugin'].getlist('conf_mgmt.skills', '.SYS', '|'):
        [repo, sk] = elt.split('.')
        repos[sk] = repo
    if skill in repos.keys():
        return repos[skill]
    else:
        return ''

def get_req():
    """ We fetch the request backwards by using the callback stack """
    frame = inspect.currentframe()
    while frame and 'req' not in frame.f_locals:
        frame = frame.f_back
    return frame.f_locals['req'] if frame else None


def repo_path(in_url):
    match = re.match(r'(?:/(\w+))?(/(?:trunk|tags|branches))(/.+)', in_url)
    if match:
        return '%s%s' % (match.group(2), match.group(3))
    else:
        return None


def get_url(in_url):
    """ Returns input url without revision or more generally without query string """
    if in_url:
        index = in_url.rfind('?')
        if index != -1:
            out_url = in_url[:index]
        else:
            index = in_url.rfind('@')
            if index != -1:
                out_url = in_url[:index]
            else:
                out_url = in_url
    else:
        out_url = ''
    return out_url


def get_revision(in_url):
    """ Returns input url revision """
    if in_url:
        index = in_url.rfind('?')
        if index != -1:
            qstring = in_url[index + 1:]
            args = parse_query_string(qstring)
            if 'rev' in args:
                rev = args['rev']
            else:
                rev = ''
        else:
            index = in_url.rfind('@')
            if index != -1:
                rev = in_url[index + 1:]
            else:
                rev = ''
    else:
        rev = ''
    return rev


def get_date(env, in_url):
    """ Returns input url date """
    if in_url:
        rev = get_revision(in_url)
        repos=get_repository(env, in_url)
        changeset = repos.get_changeset(rev)
        date = changeset.date
    else:
        date = None
    return date


def url_from_browse(env, browse_url, regexp):
    match = re.search(regexp, browse_url)
    if match and not match.group(2):
        # No query string - so this is HEAD revision
        # and we want the last commited revision
        url = match.group(1)
        url += '?rev=' + get_last_path_rev_author(env, url)[2]
    elif match and match.group(2):
        # Url without revision
        url = match.group(1)
        # Args from query string
        args = parse_query_string(match.group(2))
        if 'rev' not in args or args['rev'] == '':
            # So this is HEAD revision
            # and we want the last commited revision
            args['rev'] = get_last_path_rev_author(env, url)[2]
        url += '?rev' + '=' + args['rev']
    return url


def get_path(path_info):
    regular_expression = r"/browser/(?:[^/]+/)?(trunk)(/.*)?$"
    match = re.search(regular_expression, path_info)
    if match:
        return [match.group(1), match.group(2)]
    else:
        regular_expression = r"/browser/(?:[^/]+/)?(branches)/(B\d+)"
        match = re.search(regular_expression, path_info)
        if match:
            return [match.group(1), match.group(2)]
        else:
            regular_expression = r"/browser/(?:[^/]+/)?(tags)/(versions|milestones)/"
            match = re.search(regular_expression, path_info)
            if match:
                return [match.group(1), match.group(2)]
            else:
                return None


def repo_url(href):
    regular_expression = r"/tracs/\w+/browser((/(?![trunk|tags|branches])\w+)?(/trunk|/tags|/branches)?(/(?![trunk|tags|branches]).+)?)"
    match = re.search(regular_expression, href)
    if match and match.group(1):
        return match.group(1)
    else:
        return '/'


def exist_in_repo(env, url):
    """
        Test existence of the given url (http(s)://...) in the repository
    """
    if url:
        unix_cmd_list = [SVN_TEMPLATE_CMD % {'subcommand': 'info'} +
                         '"%s" &> /dev/null' % get_url(url)]
        retcode = unix_cmd_apply(env, unix_cmd_list, lineno())[0]
        if retcode != 0:
            return False
        else:
            return True
    else:
        return False


def node_is_dir(env, url):
    repos = get_repository(env, url)
    if repos:
        if repos.reponame:
            node_url = url[len(repos.reponame) + 1:]
        else:
            node_url = url
        try:
            node = repos.get_node(unicode_unquote_plus(get_url(node_url).encode('utf-8')),
                                  get_revision(node_url))
        except NoSuchNode:
            return False
        except Exception as e:
            raise TracError(e)
        return node.isdir
    else:
        return False

def inplace(orig_path, encoding='utf-8'):
    """Modify a file in-place, with a consistent encoding."""
    new_path = orig_path + '.modified'
    with codecs.open(orig_path, encoding=encoding) as orig:
        with codecs.open(new_path, 'w', encoding=encoding) as new:
            for line in orig:
                yield line, new
    os.rename(new_path, orig_path)

def is_word_file(filename):
    return os.path.splitext(filename)[1] in ['.docx', '.docm', '.doc', '.rtf']


def is_int(s):
    try:
        int(s)
        return True
    except ValueError:
        return False


def is_branch_name(name):
    match = False
    regular_expression = r"(B\d+)"
    if re.search(regular_expression, name):
        match = True
    else:
        match = False

    return match


def get_branch(url):
    if url:
        match = re.search(r"/branches/(B\d+)/", url)
        if match:
            return match.group(1)
        else:
            return 'trunk'
    else:
        return None


def get_skill(env, name, prefix):
    # known skills
    skills = env.config.get('ticket-custom', 'skill.options')
    # Skill is extracted from name
    regular_expression = r"\A%s\_(%s)\_" % (prefix, skills)
    m = re.search(regular_expression, name)
    if m:
        skill = m.group(1)
    else:
        skill = env.config.get('artusplugin', 'default_skill', 'SYS')
    return skill


def is_simple_component(env, name, prefix):
    skill = get_skill(env, name, prefix)
    code_shortname = get_prop_values(env, 'code_shortnames')[skill]
    return code_shortname in name

def skill_is_unmanaged(env, name):
    program_name = get_program_data(env)['program_name']
    unmanaged_skills = env.config.get('artusplugin', 'unmanaged_skills', 'EXT')
    regexp = '^%s_(%s)_'  % (program_name, unmanaged_skills)
    if re.search(regexp, name):
        return True
    else:
        return False

def get_milestone_skills(env, skill):
    """
    For a given ticket skill return the compatible milestone skills
    e.g.: 'HW'-> ['SYS', 'HW']
    :param env: Trac environment
    :param skill: ticket skill
    :type skill: string
    """
    if env.config.get('ticket-custom', 'skill.options').strip() == '':
        return []
    milestone_filter_string = env.config.get(
        'artusplugin',
        'milestone_filter').strip()
    if milestone_filter_string == '':
        env.log.error("Bad configuration for 'milestone_filter'"
                      " in get_milestone_skills")
        raise TracError(_('Configuration error. Please contact the TRAC admin.'))
    for flagged_skills in [x.strip()
                           for x in milestone_filter_string.split('//')]:
        field_value, skills_list = [y.strip()
                                    for y in flagged_skills.split('->')]
        if field_value == skill:
            break
    else:
        env.log.error("Skill '%s' not found in definition of 'milestone_filter'"
                      " in get_milestone_skills" % skill)
        raise TracError(_('Configuration error. Please contact the TRAC admin.'))
    skills = [x.strip() for x in skills_list.split(',')]
    assert(skills)
    return skills


def get_ticket_skills(env, skill):
    """
    For a given milestone skill return the compatible ticket skills
    e.g.: 'SYS'-> ['SYS', 'HW', 'FW', 'INDUS']
    :param env:
    :param skill:
    :type skill:
    """
    skills = []
    milestone_filter_string = env.config.get(
        'artusplugin',
        'milestone_filter').strip()
    if milestone_filter_string == '':
        env.log.error("Bad configuration for 'milestone_filter'"
                      " in get_milestone_skills")
        raise TracError(_('Configuration error. Please contact the TRAC admin.'))
    for flagged_skills in [x.strip()
                           for x in milestone_filter_string.split('//')]:
        field_value, skills_list = [y.strip()
                                    for y in flagged_skills.split('->')]
        if skill in [x.strip() for x in skills_list.split(',')]:
            skills.append(field_value)
    assert(len(skills) == len(set(skills)))
    return skills


def has_coherent_skill(env, item, filter_list):
    program_name = get_program_data(env)['program_name']
    return get_skill(env, item, program_name) in filter_list


def get_filtered_items(env, items, filter_list):
    return sorted([item for item in items
                   if has_coherent_skill(env, item, filter_list)])


def get_prop_values(env, prop_name):
    # Breaks a property conditional value into a dictionary of
    # (key, value) pairs
    # The conditional value is structured as follows:
    # prop_name =
    #     key11[,key12,...] -> value1 // key21[,key22,...] -> value2 // ...
    # The order is preserved for regex use
    options = OrderedDict()
    prop_value = env.config.get('artusplugin', prop_name)
    if prop_value:
        for option in [x.strip() for x in prop_value.split('//')]:
            keys, value = [y.strip() for y in option.split('->')]
            for key in [z.strip() for z in keys.split(',')]:
                options[key] = value

    return options


def get_head_revision(repos):
    # Get HEAD revision (string)
    return str(repos.get_youngest_rev())


def get_repository(env, url):
    # Get repository from url
    regular_expression = r"/(?:(\w+)/)?(?:trunk|tags|branches)(?:/.*)?"
    match = re.search(regular_expression, url)
    if match:
        rm = RepositoryManager(env)
        repos = rm.get_repository(match.group(1))
        return repos
    else:
        return None


def get_last_path_rev_author(env, in_url, in_rev='', resync=True):
    """ Gets nearest path, revision and author to the revision:
            * given in 'in_rev' (HEAD by default) if resync is False
            * HEAD if resync is True (default value)
        by scanning the associated log and the base path log when resyncing
        (whatever the base path: trunk, branches or tags)
        Note: it works only in the base path context
          IN:
            in_url : [/reponame]/trunk/... or [/reponame]/branches/... or [/reponame]/tags/... (directory) (WITHOUT rev)
            in_rev : revision
          OUT:
            repos:  the identified repository (empty if default repository)
            path:   nearest path as described above. Ex:
                    /tags/... (without revision)
                    /branches/... (without revision)
                    /trunk/... (without revision)
            rev:    associated revision
            author: associated author
    """

    # Return values if nothing works
    path = ''
    rev = ''
    author = ''

    # Analysis of input parameter: in_url
    match = re.match(r'(?:/(\w+))?(/(?:trunk|tags|branches))(/.*)?$', in_url)

    if match:
        reponame = match.group(1)
        rm = RepositoryManager(env)
        repos = rm.get_repository(reponame)
        head_revision = str(repos.repos.youngest_rev)
        path_base = match.group(2)
        sub_path = match.group(3)
        path = path_base + sub_path if sub_path else path_base
        rev = in_rev or head_revision
    else:
        return '', path, rev, author

    if resync:
        # Accumulate all moves
        renamed_paths = []
        for rv in range(int(rev), int(head_revision)):
            for change in repos.get_changeset(rv).get_changes():
                # COPY is added ALTHOUGH MOVE should be indicated for a rename: to investigate (TortoiseSVN deletes/add, TRAC gives EDIT/COPY ??)
                if change[1] == Node.DIRECTORY and (change[2] == Changeset.MOVE or change[2] == Changeset.COPY) and change[0].startswith(path_base[1:]) and change[3].startswith(path_base[1:]):
                    renamed_paths.append((change[3], change[0]))

        # We now make any relevant substition in the url from oldest one to newest one
        for old_path, new_path in renamed_paths:
            if old_path in path:
                path = path.replace(old_path, new_path)
        revision = head_revision
    else:
        revision = rev

    # We now search backwards through the history starting @ 'revision' for the exact revision
    if repos.has_node(path, revision):
        for h in repos.get_node(path, revision).get_history(1):
            rev = str(h[1])
            author = repos.get_changeset(int(rev)).author
            break

    return repos.reponame, path, rev, author


def analyse_url(env, url):
    """
    Returns splitted url (paths) and none existent components (dirs)
    url has no revision specified
    url: [/reponame]/[trunk|tags|branches]/...
    """
    paths = []
    path = url
    regular_expression = r"(?:/\w+)?/(?:trunk|tags|branches)(?:/.*)?"
    while re.search(regular_expression, path):
        paths.insert(0, get_repo_url(env, path))
        path = path[:path.rfind('/')]
    dirs = []
    repos = get_repository(env, url)
    offset = paths[0].rfind('/')
    for path in paths[:-1]:
        if not repos.has_node(path[offset:], ''):
            dirs.append(path)
    return paths, dirs


def unix_cmd_apply(env, unix_cmd_list, line_number, nesting_level=0):
    """Apply a list of Unix commands and returns the error code
    and the stdout/stderr output of the last executed command."""
    if env:
        log_level = env.config.get('logging', 'log_level')
    else:
        log_level = "ERROR"
    retcode = 0
    lines = []
    if unix_cmd_list:
        try:
            for unix_cmd in unix_cmd_list:
                # each command is executed in turn
                p1 = subprocess.Popen(unix_cmd.encode('utf-8'),
                                      shell=True,
                                      cwd=apache_homedir,
                                      stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT,
                                      env={'LC_ALL': 'fr_FR.utf8',
                                           'HOME': apache_homedir,
                                           'PYTHONIOENCODING': 'utf-8'})
                retcode = p1.wait()
                # failure or success ?
                failed = retcode != 0
                # test command
                if "&> /dev/null" in unix_cmd:
                    continue
                # command output
                child_stdout_and_stderr = p1.stdout
                for line in child_stdout_and_stderr.readlines():
                    lines += str(line)

                if failed:
                    # lookup for special errors
                    missing_pristine = False
                    cleanup = False
                    for line in lines:
                        if u'texte de référence' in line:
                            # E155010 E155032
                            start_idx = unix_cmd.find('/var/cache/trac/tickets/')
                            end_idx = start_idx + re.search(
                                r'[";]', unix_cmd[start_idx:]).start()
                            wc_path = unix_cmd[start_idx:end_idx]
                            if wc_path:
                                if os.path.isfile(wc_path):
                                    wc_path = '/'.join(wc_path.split('/')[:-1])
                                sha1 = line[line.find(" '") + 2:line.find("' ")]
                                if len(sha1) == 40:  # 160 bits/40 hexadecimal digits
                                    missing_pristine = True
                        elif u'verrouillée' in line:
                            # E155004
                            wc_path = line[line.find("/var/cache/trac/tickets/"):
                                           line.rfind(u"'")]
                            if wc_path:
                                cleanup = True

                    # before trying to restore things, we wait a while (it may be concurrency)
                    sleep(1)
                    # let's try
                    if missing_pristine:
                        # we try to restore the missing pristine
                        my_unix_cmd_list = ['cd "%s";/srv/svn/common/svn-fetch-pristine-by-sha1.sh %s' % (wc_path, sha1)]
                        retcode, lines = unix_cmd_apply(env, my_unix_cmd_list, lineno())
                        if retcode == 0 and nesting_level == 0:
                            # retry failed command
                            my_unix_cmd_list = [unix_cmd]
                            retcode, lines = unix_cmd_apply(env, my_unix_cmd_list, lineno(), 1)
                            failed = retcode != 0
                    if cleanup:
                        # a cleanup is executed before retrying
                        my_unix_cmd_list = [SVN_TEMPLATE_CMD % {'subcommand': 'cleanup'} + '"%s"' % wc_path]
                        retcode, lines = unix_cmd_apply(env, my_unix_cmd_list, lineno())
                        if retcode == 0 and nesting_level == 0:
                            # retry failed command
                            my_unix_cmd_list = [unix_cmd]
                            retcode, lines = unix_cmd_apply(env, my_unix_cmd_list, lineno(), 1)
                            failed = retcode != 0

                # command output log
                if failed:
                    msg = ("The following command failed "
                           "(retcode = %s - line number = %s - process id = %s - thread id = %s):" %
                           (retcode, line_number, os.getpid(), current_thread()))
                elif log_level == 'INFO':
                    msg = ("The following command succeeded "
                           "(retcode = %s - line number = %s - process id = %s - thread id = %s):" %
                           (retcode, line_number, os.getpid(), current_thread()))
                if failed or log_level == 'INFO':
                    output_header_printed = False
                    syslog.syslog(msg)
                    syslog.syslog("    " + unix_cmd.encode('utf-8'))
                    for line in [l.encode('utf-8') for l in lines]:
                        if output_header_printed is False:
                            syslog.syslog("with the following output:")
                            output_header_printed = True
                        syslog.syslog("    " + line)

                # Abort the execution of commands
                if failed:
                    break

        except OSError as e:
            msg = "Execution failed: %s at %s" % (e, line_number)
            syslog.syslog(msg)
            retcode = -1

    return (retcode, lines)


def lineno():
    """Returns the current line number in our program."""
    caller_frame = inspect.currentframe().f_back
    filename = caller_frame.f_code.co_filename
    relativefn = filename[filename.find('artusplugin'):]
    line_no = caller_frame.f_lineno
    return relativefn + ':' + str(line_no)


def caller_lineno():
    """Returns the current line number in our program."""
    caller_frame = inspect.currentframe().f_back.f_back
    filename = caller_frame.f_code.co_filename
    relativefn = filename[filename.find('artusplugin'):]
    line_no = caller_frame.f_lineno
    return relativefn + ':' + str(line_no)


def get_ticket_number(filename):
    try:
        file_no = open(filename, 'rb')
        line = file_no.readline()
        file_no.close()
        return int(line)
    except IOError:
        return None


def set_ticket_number(filename, number):
    file_sema = BoundedSemaphore(value=1)
    file_sema.acquire()
    try:
        file_no = open(filename, 'r+b')
        file_no.seek(0)
        file_no.truncate()
    except IOError:
        file_no = open(filename, 'wb')
    line = str(number)
    file_no.write(line)
    file_no.close()
    file_sema.release()


def new_ticket_number(filename):
    """Allocate a new ticket number."""
    number = get_ticket_number(filename)
    if number:
        number += 1
    else:
        number = 1
    set_ticket_number(filename, number)
    return number


def del_ticket_number(filename, number):
    """Deallocate a ticket number."""
    if number == get_ticket_number(filename):
        # Only last allocated ticket will be deallocated
        # So if several tickets have to be deleted
        # remove them from latest backwards
        if number:
            number -= 1
        else:
            number = 0
        set_ticket_number(filename, number)
    return number


def child_tickets_for_tag(ticket, tgname=None):
    env = ticket.env
    ctks = []
    chldtktids = childtickets.childtickets.TracchildticketsModule(env).childtickets.get(ticket.id, [])
    try:
        parent_tag = ticket['document'] if tgname is None else tgname
        for ctid in chldtktids:
            ctk = Ticket(env, ctid)
            try:
                child_tag = ctk['document']
            except ResourceNotFound:
                continue
            if parent_tag == child_tag:
                ctks.append(ctk)
    except ResourceNotFound:
        pass
    return ctks


def get_trunk_url_rev_from_tag(env, ticket):
    """ Return the subversion url and revision in the trunk or branch associated to the tag url and revision given by 'documenturl'
        The revision is the repository revision at the time the tag was created.
        So it may be different from the last committed revision of the document in the trunk or branch. """

    trunkurl = ''
    trunkrev = ''

    if ticket['documenturl'] != "":

        tagurl = get_url(ticket['documenturl'])
        tagrev = get_revision(ticket['documenturl'])
        repos = get_repository(env, tagurl)

        # Get the document url under '/tags'. We want the last path even through path moves
        if not repos.has_node(tagurl, tagrev):
            last_path_rev_author = get_last_path_rev_author(env, tagurl, '1')
            reponame = last_path_rev_author[0]
            tagurl = last_path_rev_author[1]
            tagrev = last_path_rev_author[2]
        else:
            reponame = repos.reponame

        # Get the document url and rev under '/trunk' or '/branches'
        urlatrev = tagurl
        if reponame:
            urlatrev = '/' + reponame + urlatrev
        if tagrev:
            urlatrev += '@' + tagrev
        unix_cmd_list = [SVN_TEMPLATE_CMD % {'subcommand': 'log --verbose '} +
                         '"' + get_repo_url(env, urlatrev) +
                         '"|grep -P "\(de /(trunk|branches)" | awk \'{print}\'']

        # Effective application of the list of commands
        lines = unix_cmd_apply(env, unix_cmd_list, lineno())[1]

        if len(lines) != 0:
            line = lines[0]
            # This is an url without revision
            trunkurl = line[line.find("(de /"):line.find(":")]
            trunkurl = trunkurl[trunkurl.find("/"):]
            trunkrev = line[line.find(":") + 1:line.find(")")]
            if reponame:
                trunkurl = '/' + reponame + trunkurl
        else:
            # Case of EOC when built with buildbot:
            # tag is not created from trunk or branch
            trunkurl = tagurl
            if reponame:
                trunkurl = '/' + reponame + trunkurl
            trunkrev = tagrev

    return trunkurl, trunkrev


def get_tracbrowserurl(env, in_url=None, caller=None, admin_branch=None):
    """ Returns an url compatible with the TRAC browser
        If in_url is not given, the repository root url is returned
        The query string is preserved (eg: the revision)
        If passed, caller is added to the query string """
    out_url = env.base_url + '/browser'
    args = None
    if in_url:
        out_url += in_url
        qidx = in_url.find('?')
        if qidx != -1:
            qstring = in_url[qidx + 1:]
            args = parse_query_string(qstring)
    if caller:
        if args and len(args) > 0:
            out_url += '&caller=' + unicode_quote_plus(caller)
        else:
            out_url += '?caller=' + unicode_quote_plus(caller)
    if admin_branch:
        if args and len(args) > 0:
            out_url += '&admin_branch=' + admin_branch
        else:
            out_url += '?admin_branch=' + admin_branch
    return out_url


def get_trac_browser_url(env, in_url, revision):
    return '%s/browser%s?format=raw&rev=%s' % (env.base_url, in_url, revision)


def get_revision_from_description(summary, description):
    """ Get ticket form revision """
    revision = ''
    if description:
        re_string = r'.+%s\?rev=(\d+) ' % summary
        m = re.search(re_string, description)
        if m:
            revision = m.group(1)
    return revision


def get_ecm_tktid(env, version):
    db = env.get_db_cnx()
    cursor = db.cursor()
    cursor.execute("SELECT id FROM ticket "
                   "WHERE ticket.summary='%s'" % version)
    row = cursor.fetchone()
    if row:
        return row[0]
    else:
        return None


def get_ecm_tktstatus(env, version):
    db = env.get_db_cnx()
    cursor = db.cursor()
    cursor.execute("SELECT status FROM ticket "
                   "WHERE ticket.summary='%s'" % version)
    row = cursor.fetchone()
    if row:
        return row[0]
    else:
        return None


def get_fee_tktid(env, version):
    db = env.get_db_cnx()
    cursor = db.cursor()
    cursor.execute("SELECT id FROM ticket "
                   "WHERE ticket.summary='%s'" % version)
    row = cursor.fetchone()
    if row:
        return row[0]
    else:
        return None


def get_fee_tktstatus(env, version):
    db = env.get_db_cnx()
    cursor = db.cursor()
    cursor.execute("SELECT status FROM ticket "
                   "WHERE ticket.summary='%s'" % version)
    row = cursor.fetchone()
    if row:
        return row[0]
    else:
        return None
    

def exist_doc_tktid(env, ci):
    db = env.get_db_cnx()
    cursor = db.cursor()
    cursor.execute("SELECT substr(summary, 5) FROM ticket "
                   "WHERE substr(summary, 5) LIKE "
                   "'%s_%%'" % ci)
    row = cursor.fetchone()
    if row:
        if row[0].rsplit('_', 1)[0] == ci:
            return True
        else:
            return False
    else:
        return False


def get_doc_tktid(env, version):
    db = env.get_db_cnx()
    cursor = db.cursor()
    cursor.execute("SELECT id FROM ticket "
                   "WHERE ticket.summary='DOC_%s'" % version)
    row = cursor.fetchone()
    if row:
        return row[0]
    else:
        return None


def get_doc_tktstatus(env, version):
    db = env.get_db_cnx()
    cursor = db.cursor()
    cursor.execute("SELECT status FROM ticket "
                   "WHERE ticket.summary='DOC_%s'" % version)
    row = cursor.fetchone()
    if row:
        return row[0]
    else:
        return None


def get_doc_skill(env, name, prefix):
    """
    name: a CI name, version name or version tag name
    """
    doc_skill = None

    if name:
        skill = get_skill(env, name, prefix)
        DOC_skills = env.config.get('artusplugin',
                                    'DOC_skills', '').split('|')
        if skill in DOC_skills:
            doc_skill = skill

    return doc_skill


def get_doc_query_string(env, skill, ci):
    DOC_skills = env.config.get('artusplugin',
                                'DOC_skills', '').split('|')
    DOC_report = env.config.get('artusplugin', 'DOC_report')
    if skill in DOC_skills:
        db = env.get_db_cnx()
        cursor = db.cursor()
        cursor.execute("SELECT query FROM report "
                       "WHERE id=%s" % DOC_report)
        row = cursor.fetchone()
        if row:
            query_string = re.sub(r'skill=.*?\n', 'skill=%s\n' % skill, row[0])
            query_string = ('?configurationitem=%s&' % ci +
                            query_string.replace('query:?', '') +
                            '&report=%s' % DOC_report)
            return query_string
        else:
            return None
    else:
        return None


def get_mom_tktid(env, milestone_tag):
    db = env.get_db_cnx()
    cursor = db.cursor()
    if milestone_tag.status == 'Prepared':
        sql = ("SELECT id FROM ticket t, ticket_custom tc1, ticket_custom tc2 "
               "WHERE t.id=tc1.ticket AND t.id=tc2.ticket "
               "AND t.type='MOM'"
               "AND tc1.name='milestonetag' "
               "AND tc1.value='%s' "
               "AND tc2.name='momtype' "
               "AND tc2.value='CCB'" % milestone_tag.name)
    elif milestone_tag.status == 'Reviewed':
        sql = ("SELECT id FROM ticket t, ticket_custom tc1, ticket_custom tc2 "
               "WHERE t.id=tc1.ticket AND t.id=tc2.ticket "
               "AND t.type='MOM'"
               "AND tc1.name='milestonetag' "
               "AND tc1.value='%s' "
               "AND tc2.name='momtype' "
               "AND tc2.value='Review'" % milestone_tag.name)
    else:
        return None
    cursor.execute(sql)
    row = cursor.fetchone()
    if row:
        return row[0]
    else:
        return None


def get_edit_or_view_mode(env, req, ticket, permission):
    """ Compute if ticket or attachment should be proposed
        for edition or only for viewing. It is based on
        work-flow status, ticket ownership and permission """

    if (ticket['status'] == '01-assigned_for_description' or
        ticket['status'] == '01-assigned_for_edition' or
        ticket['status'] == '03-assigned_for_analysis' or
        (ticket['type'] in ('RF', 'PRF') and
         ticket['status'] == '06-implemented') or
        (ticket['type'] == 'ECR' and
         ticket['status'] == '06-implemented' and
         env.config.get('artusplugin',
                        '%s_%s_regression_analysis' % (
                            ticket['skill'],
                            ticket['type'])) == '1')):
        status_ok = True
    else:
        status_ok = False

    if ticket['owner'] == req.authname:
        user_ok = True
    else:
        user_ok = False

    perm = PermissionSystem(env)
    all_permissions = perm.get_all_permissions()
    if ((req.authname, 'admin') in all_permissions or
        (req.authname, permission) in all_permissions):
        # admin user or TICKET_FORCE_EDIT or ATTACHMENT_FORCE_EDIT
        group_ok = True
    else:
        group_ok = False

    if status_ok is True and user_ok is True:
        if ticket['type'] == 'MOM' and permission == 'TICKET_FORCE_EDIT':
            if req.args.get('forced') == 'True':
                # Editing allowed because locked
                edit_mode = True
                # 'Lock' check-box is shown
                force_mode = True
                # 'Lock' check-box is checked
                checked = True
            else:
                # Editing not allowed because not locked
                edit_mode = False
                # 'Lock' check-box is shown)
                force_mode = True
                # 'Lock' check-box is not checked
                checked = False
        else:
            # Edit button is shown - nominal case
            edit_mode = True
            # 'Force mode' check-box is not shown (useless)
            force_mode = False
            # 'Force mode' check-box check status has no meaning
            checked = None
    elif group_ok is True:
        if req.args.get('forced') == 'True':
            # Edit button is shown - 'Force mode' required
            edit_mode = True
            # 'Force mode' check-box is shown
            force_mode = True
            # 'Force mode' check-box is checked - 'Force mode' required
            checked = True
        else:
            # Edit button is not shown - 'Force mode' not required
            edit_mode = False
            # 'Force mode' check-box is shown
            force_mode = True
            # 'Force mode' check-box is not checked - 'Force mode' not required
            checked = False
    else:
        # View button is shown
        edit_mode = False
        # User has no access to 'Force mode' - 'Force mode' check-box is not shown
        force_mode = False
        # 'Force mode' check-box check status has no meaning
        checked = None

    return edit_mode, force_mode, checked


def url_add_params(url, list_param=None):
    if url and list_param:
        for param_name, param_value in list_param:
            if param_value and isinstance(param_value, str):
                url += '&' if '?' in url else '?'
                url += param_name + '=' + param_value
    return url


def entries_add_params(req, list_param=None):
    if list_param:
        ctxtnav = req.chrome.get('ctxtnav')
        for entry in [ent for ent in ctxtnav]:
            ctxtnav.pop(0)
            href, value = get_link(entry)
            if href:
                href = url_add_params(href, list_param)
                add_ctxtnav(req, _(value), href=href)
            else:
                ctxtnav.append(entry)


def get_link(elt):
    if isinstance(elt, Element):
        if elt.tag.localname == 'a':
            return elt.attrib.get('href'), elt.children[0]
        else:
            for child in elt.children:
                href, value = get_link(child)
                if href:
                    return href, value
                else:
                    continue
            return None, None
    else:
        return None, None


def group_by(elt_list, group1=(None, True), group2=(None, True), group3=(None, True)):
    """ Group a list of elements into sub-lists sharing a common value for field: groupx[0], and sort them if groupx[1] is True """
    grouped_by_list = []
    field1 = group1[0]
    if field1:
        seen = set()
        field1_list = [elt.__getattribute__(field1) for elt in elt_list if elt.__getattribute__(field1) not in seen and not seen.add(elt.__getattribute__(field1))]
        if group1[1]:
            field1_list.sort()
        for f1 in field1_list:
            grouped_by_f1_list = []
            field2 = group2[0]
            if group2[0]:
                seen = set()
                field2_list = [elt.__getattribute__(field2) for elt in elt_list if elt.__getattribute__(field2) not in seen and not seen.add(elt.__getattribute__(field2))]
                if group2[1]:
                    field2_list.sort()
                for f2 in field2_list:
                    grouped_by_f2_list = []
                    field3 = group3[0]
                    if field3:
                        seen = set()
                        field3_list = [elt.__getattribute__(field3) for elt in elt_list if elt.__getattribute__(field3) not in seen and not seen.add(elt.__getattribute__(field3))]
                        if group3[1]:
                            field3_list.sort()
                        for f3 in field3_list:
                            grouped_by_f3_list = []
                            for elt in elt_list:
                                if elt.__getattribute__(field1) == f1 and elt.__getattribute__(field2) == f2 and elt.__getattribute__(field3) == f3:
                                    grouped_by_f3_list.append(elt)
                            if grouped_by_f3_list:
                                grouped_by_f2_list.append(grouped_by_f3_list)
                    else:
                        for elt in elt_list:
                            if elt.__getattribute__(field1) == f1 and elt.__getattribute__(field2) == f2:
                                grouped_by_f2_list.append(elt)
                    if grouped_by_f2_list:
                        grouped_by_f1_list.append(grouped_by_f2_list)
            else:
                for elt in elt_list:
                    if elt.__getattribute__(field1) == f1:
                        grouped_by_f1_list.append(elt)
            if grouped_by_f1_list:
                grouped_by_list.append(grouped_by_f1_list)
    else:
        grouped_by_list = elt_list

    return grouped_by_list


def create_fs_from_repo(path, node):
    try:
        path += '/' + node.name
        if node.isdir:
            if not os.access(path, os.F_OK):
                os.mkdir(path)
            for entry in node.get_entries():
                create_fs_from_repo(path, entry)
        else:
            with open(path.encode('utf-8'), 'w') as targetfile:
                shutil.copyfileobj(node.get_content(), targetfile)
    except Exception:
        exc_info = sys.exc_info()
        exc_obj = exc_info[1]
        exc_tb = exc_info[2]
        fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
        syslog.syslog("Unexpected error: %s:%s %s" % (fname, exc_tb.tb_lineno, exc_obj))

def format_img(data):
    data_img = urllib2.urlopen('%s/htdocs/%s' % (data['host_url'], data['image'])).read()
    return data_img


def format_html(env, data):
    chrome = Chrome(env)
    dirs = []
    for provider in chrome.template_providers:
        dirs += provider.get_templates_dirs()
    templates = TemplateLoader(dirs, variable_lookup='lenient')

    _buffer = cStringIO()
    try:
        template = templates.load('pdf_printing_job.html', cls=MarkupTemplate)
        if template:
            stream = template.generate(**data)
            stream.render('xhtml', doctype=DocType.XHTML_STRICT, out=_buffer)
    except TemplateNotFound:
        pass

    return _buffer.getvalue()


def format_text(data):

    text = (u"You have submitted a job of ticket(s) conversion to PDF. The job has just completed.\n\n"
            u"The tickets attachments, when they exist, are attached to the PDF files (ie included).\n\n"
            u"Here is the link to download the zip file containing the PDF files:\n\n"
            u"%s\n\n"
            u"This link will remain valid at least for 10 days. After that, the zip file will be removed.\n\n"
            u"You may reply to this mail if something goes wrong.\n\n"
            u"TRAC Admin")  % data['link']

    return text


def send_pdf_print_email(env, authname, base_path):
    """Send a MIMEMultipart message."""
    hostname = get_hostname(env)
    host = hostname.split('.')[0]
    scheme = env.config.get('artusplugin', 'scheme')
    host_url = '%s://%s' % (scheme, hostname)
    project_name = env.project_name
    project_desc = env.project_description
    smtp_from_name = env.config.get('announcer', 'smtp_from_name')
    smtp_from = env.config.get('announcer', 'smtp_from')
    (head, tail) = os.path.split(base_path)
    users_permissions = Users(env)
    data = dict(
        scheme=scheme,
        host_url=host_url,
        project_name = project_name,
        project_desc = project_desc,
        host = host
    )

    data['link'] = '%s://%s/tracs/%s/PDF-printing/%s.zip' % (
        scheme,
        hostname,
        os.path.basename(head),
        tail)

    msg = MIMEMultipart()

    # email body
    html = format_html(env, data)
    if html:
        part = MIMEText(html, 'html', 'utf-8')
    else:
        text = format_text(data)
        part = MIMEText(text, 'plain', 'utf-8')
    msg.attach(part)

    trac_version = get_pkginfo(trac.core).get('version', trac.__version__)
    artusplugin_version = get_pkginfo(artusplugin).get('version', 'Undefined')

    msg['X-Mailer'] = 'ArtusPlugin v%s on Trac v%s' % (
        artusplugin_version,
        trac_version)
    msg['X-Trac-Version'] = trac_version
    msg['X-ArtusPlugin-Version'] = artusplugin_version
    msg['X-Trac-Project'] = env.project_name
    msg['Precedence'] = 'bulk'
    msg['Auto-Submitted'] = 'auto-generated'
    msg['Accept-Language'] = 'en-GB'
    msg['Content-Language'] = 'en-GB'

    def get_address(username):
        with Ldap_Utilities() as ldap_util:
            email = Users.get_email(env, username, ldap_util)
        displayname = users_permissions.users_ldap_names[username]
        displayname = str(displayname)
        displayname = '"%s"' % unidecode(displayname)
        return (displayname, email)

    from_user = '%s <%s>' % get_address(authname)
    to_user = from_user
    msg['Subject'] = "TRAC PDF printing job: %s" % tail
    msg['From'] = from_user
    msg['Sender'] = '"%s" <%s>' % (smtp_from_name, smtp_from)
    msg['To'] = to_user
    admin = env.project_admin.split('@')[0]
    msg['Reply-To'] = '%s <%s>' % get_address(admin)

    s = smtplib.SMTP('localhost')
    s.sendmail(from_user, [to_user], msg.as_string())
    s.quit


def pdf_attach_files(pdf_input_file, attachments, pdf_output_file):
    """ Add the given attachments to the given input pdf file.
        The attachments'paths are given in a list
        The output pdf file shall be different from the input pdf file
    """
    bookmark_file = tempfile.mkstemp('.csv')[1]

    unix_cmd_list = ['java -jar /opt/jpdftweak/jpdftweak.jar -i "%s" ' % pdf_input_file +
                     ' -savebookmarks "%s" -o "%s"' % (bookmark_file, pdf_output_file)]

    unix_cmd_list += ['java -jar /opt/jpdftweak/jpdftweak.jar -i "%s" ' % pdf_input_file +
                      ' '.join(['-attach "%s"' % attachment for attachment in attachments]) +
                      ' -pagemode Attachments -loadbookmarks "%s" -o "%s"' % (bookmark_file, pdf_output_file)]

    # Effective application of the list of commands
    retcode, lines = unix_cmd_apply(None, unix_cmd_list, lineno())

    os.remove(bookmark_file)

    if retcode != 0:
        raise TracError(''.join(lines))


def strip_accents(text):
    """
    Strip accents from input String.

    :param text: The input string.
    :type text: String.

    :returns: The processed String.
    :rtype: String.
    """
    try:
        text = str(text)
    except (TypeError, NameError): # unicode is a default on python 3 
        pass
    text = unicodedata.normalize('NFD', text)
    text = text.encode('ascii', 'ignore')
    text = text.decode("utf-8")
    return str(text)


class ArtusDomainEmailResolver(Component):
    """Support of new email scheme for old repositories."""

    implements(IAnnouncementAddressResolver)

    smtp_default_domain = Option('announcer', 'smtp_default_domain', '',
                                 """Default host/domain to append to address"""
                                 """ that do not specify one""")

    translation_file = Option('artusplugin', 'translation_file')

    domain_translation = {}

    name_translation = {}

    def __init__(self):
        meggitt_translation = ConfigParser.RawConfigParser()
        meggitt_translation.read(self.translation_file)
        self.domain_translation = dict(meggitt_translation.items('domain-translation'))
        self.name_translation = dict(meggitt_translation.items('user-translation'))

    def get_address_for_name(self, name, authenticated):
        if self.smtp_default_domain:
            mydomain = self.smtp_default_domain
            myname = name
            if mydomain in self.domain_translation:
                mydomain = self.domain_translation[mydomain]
            if myname in self.name_translation:
                myname = self.name_translation[myname]
            return '%s@%s' % (myname, mydomain)

        return None


class Users(Component):
    """Compile projet users data for use by other components."""

    @staticmethod
    def get_email(env, user, ldap_util):
        email = ldap_util.get_meggitt_mail(user)
        if email:
            # User is in the company directory
            return email
        else:
            email = SpecifiedEmailResolver(env).get_address_for_name(user, True)
            if email:
                # User is external to the company
                return email
            else:
                # User is not in the company directory and not external to the company
                # it seems user has left the company
                return None

    def __init__(self):
        # List of profiles
        self.user_profiles = [profile.strip() for profile in
                              self.env.config.get('artusplugin', 'user_profiles').
                              split(',')]
        self.displayed_profiles = OrderedDict()
        for profile in self.user_profiles:
            self.displayed_profiles[profile] = profile.capitalize()
        # Sorted list of roles
        self.user_roles = sorted([role.strip() for role in
                                  self.env.config.get('artusplugin', 'user_roles').
                                  split(',')])
        self.displayed_roles = OrderedDict()
        for role in self.user_roles:
            self.displayed_roles[role] = role.replace('_', ' ').title()
        self.role_initials = {}
        for role in self.user_roles:
            if role == 'program_manager':
                self.role_initials[role] = 'PgM'
            elif role == 'project_manager':
                self.role_initials[role] = 'PjM'
            else:
                self.role_initials[role] = ''.join(item[0] for item in self.displayed_roles[role].split())

        perm = PermissionSystem(self.env)
        self.all_permissions = perm.get_all_permissions()

        # Sort by second element
        def take_second(elem):
            return elem[1]

        # Sorted list of roles by profile
        self.roles_by_profile = {}
        for profile in self.user_profiles:
            self.roles_by_profile[profile] = [subject
                                              for (subject, action) in self.all_permissions
                                              if subject not in self.user_profiles and
                                              subject in self.user_roles and
                                              action == profile]
            self.roles_by_profile[profile].sort()

        # Sorted list of users by profile
        # (directly assigned to profiles, not through roles)
        self.users_by_profile = {}
        for profile in self.user_profiles:
            self.users_by_profile[profile] = [subject
                                              for (subject, action) in self.all_permissions
                                              if subject not in self.user_profiles and
                                              subject not in self.user_roles and
                                              action == profile]
            self.users_by_profile[profile].sort()

        # Sorted list of users by role
        self.users_by_role = {}
        for role in self.user_roles:
            self.users_by_role[role] = [subject
                                        for (subject, action) in self.all_permissions
                                        if action == role]
            self.users_by_role[role].sort()

        # Sorted list of users with role by profile
        # (users with several roles in same profile are not duplicated)
        self.users_with_role_by_profile = {}
        for profile in self.user_profiles:
            self.users_with_role_by_profile[profile] = []
            for role in self.roles_by_profile[profile]:
                self.users_with_role_by_profile[profile] += \
                    [user for user in self.users_by_role[role]]
            self.users_with_role_by_profile[profile] = \
                sorted(set(self.users_with_role_by_profile[profile]))

        # Sorted list of users without role by profile
        self.users_without_role_by_profile = {}
        for profile in self.user_profiles:
            self.users_without_role_by_profile[profile] = \
                [user for user in self.users_by_profile[profile]]

        # All registered users
        htpasswd_file = self.env.config.get("artusplugin", "htpasswd_file")
        lines = open(htpasswd_file, 'r').readlines()
        self.registered_users = {line.split(':')[0] for line in lines}

        # Test users
        self.test_users = [test_user.strip() for test_user in
                           self.env.config.get('artusplugin', 'htpasswd_test_users').split(',')]

        # All project users
        self.project_users = set()
        for profile in self.users_with_role_by_profile:
            self.project_users.update(self.users_with_role_by_profile[profile])
        for profile in self.users_without_role_by_profile:
            self.project_users.update(self.users_without_role_by_profile[profile])
        sorted(self.project_users)

        # Users LdapNames
        self.users_ldap_names = OrderedDict()
        for (k,v) in sorted(UsersLdapNames(self.env).get_users_ldap_names(), key=take_second):
            self.users_ldap_names[k] = v

        # Email addresses
        self.users_emails = {}
        with Ldap_Utilities() as ldap_util:
            for user in self.project_users:
                self.users_emails[user] = Users.get_email(self.env, user, ldap_util)

    def user_check(self, user):
        # Check user is not associated with a profile or a role
        return user not in self.project_users

    def role_check(self, role):
        # Check role is associated with one and only one profile
        return len([profile for profile in self.roles_by_profile.keys() if role in self.roles_by_profile[profile]]) == 1

    def group_check(self, group):
        # Check group is a known profile or role
        return group in self.user_profiles or group in self.user_roles


class UsersLdapNames(object):
    """Support of permissions admin panel."""

    def __init__(self, env):
        self.env = env
        self.htpasswd_file = self.env.config.get('artusplugin', 'htpasswd_file')
        self.translation_file = self.env.config.get('artusplugin', 'translation_file')
        self.ldap_display_names_file = self.env.config.get('artusplugin', 'ldap_display_names_file')
        self.ldap_display_names_section = 'user-display'
        self.projects_database_filepath = self.env.config.get('artusplugin', 'projects_database_filepath')
        self.special_users = [special_user.strip() for special_user in
                              self.env.config.get('artusplugin', 'htpasswd_special_users').split(',')]
        self.login_type = self.get_project_login_type()
        self.sem_name = '/%s' % str(self.ldap_display_names_file.replace('/', ':'))
        self.sem_handle = posix_ipc.Semaphore(
            name=self.sem_name,
            flags=posix_ipc.O_CREAT,
            initial_value=1)

    def get_users_ldap_names(self):
        # Get real users
        htpasswd_file = open(self.htpasswd_file, 'r')
        lines = htpasswd_file.readlines()
        real_users = []
        for line in lines:
            user = line.split(':')[0]
            if user in self.special_users:
                continue
            real_users.append(user)

        # Start of critical section
        self.sem_handle.acquire()

        # Check cache existence and validity, regenerate if needed
        generate = False
        if not os.path.isfile(self.ldap_display_names_file):
            generate = True
        else:
            ldap_display_names_mt = os.path.getmtime(self.ldap_display_names_file)
            htpasswd_mt = os.path.getmtime(self.htpasswd_file)
            translation_mt = os.path.getmtime(self.translation_file)
            if htpasswd_mt > ldap_display_names_mt or translation_mt > ldap_display_names_mt:
                generate = True

        if generate:
            self._generate_cache(real_users)

        # End of critical section
        self.sem_handle.release()

        # Read cache
        ldap_display = ConfigParser.RawConfigParser()
        ldap_display.read(self.ldap_display_names_file)

        # Get project compatible users
        ldap_display_names = []
        for username, display_name in ldap_display.items(self.ldap_display_names_section):
            if ((self.login_type == 'fname' and '.' in username) or
                (self.login_type == 'forename.name' and '.' not in username)):
                continue
            else:
                ldap_display_names.append((username, display_name))

        return ldap_display_names

    def _generate_cache(self, usernames):
        # Get data
        ldap_display = ConfigParser.RawConfigParser()
        ldap_display.add_section(self.ldap_display_names_section)
        for username, ldap_name in self._get_ldap_names(usernames).items():
            ldap_display.set(self.ldap_display_names_section, username, ldap_name)

        # Generate
        if os.access(self.ldap_display_names_file, os.F_OK):
            os.remove(self.ldap_display_names_file)
        flags = os.O_CREAT + os.O_WRONLY
        if hasattr(os, 'O_BINARY'):
            flags += os.O_BINARY
        targetfile = os.fdopen(os.open(self.ldap_display_names_file, flags, 666), 'w')
        ldap_display.write(targetfile)
        targetfile.close()

    def _get_ldap_names(self, usernames):
        ldap_names = OrderedDict()
        with Ldap_Utilities() as ldap_util:
            for username in usernames:
                from_addr = Users.get_email(self.env, username, ldap_util)
                if from_addr:
                    user_id = from_addr.split('@')[0]
                    try:
                        ldap_name = ldap_util.get_ldap_displayname(user_id)
                        if ldap_name is None:
                            ldap_name = username
                        else:
                            ldap_name = unidecode(str(ldap_name))
                    except Exception:
                        ldap_name = username
                else:
                    ldap_name = username
                ldap_names[username] = ldap_name

        return ldap_names

    def get_project_login_type(self):
        # Get project login type:
        #   old projects: fname
        #   new projects: forename.name
        connection = sqlite3.connect(self.projects_database_filepath)
        cursor = connection.cursor()
        cursor.execute("SELECT login FROM project WHERE id='%s'" % self.env.project_description)
        row = cursor.fetchone()
        if row:
            # Get login type
            login_type = row[0]
        else:
            # Project must be in creation if not yet in the projects table
            # We infer its login type
            login_type = 'forename.name'
        connection.close()

        return login_type

class SqlServerConnection(object):
    """Connection to SQL Server."""

    def __init__(self):
        self.con_string = 'DSN=%s;UID=%s;PWD=%s;' % (ARTUS_sqlserver_data.datasource,
                                                     ARTUS_sqlserver_data.user,
                                                     ARTUS_sqlserver_data.password)
    
    def __enter__(self):
        self.cnxn = pyodbc.connect(self.con_string)
        self.cursor = self.cnxn.cursor()
        self.cursor.execute('SET QUOTED_IDENTIFIER OFF')
    
        return self.cursor

    def __exit__(self, exc_type, exc_value, traceback):
        self.cursor.execute('SET QUOTED_IDENTIFIER ON')
        self.cnxn.close()

class SqlServerView(object):
    """Extract info from SQL Server."""
    
    def get_evolrefs(self):
        with SqlServerConnection() as cursor:
            orderby = '[No DE] DESC'
            cmd = 'SELECT DISTINCT [No DE] FROM GDE_FEE_Details ORDER BY %s' % orderby
            cursor.execute(cmd)
            rows = cursor.fetchall()
            return [row[0] for row in rows]
    
    def get_customers(self, evolref):
        with SqlServerConnection() as cursor:
            where = '[No DE] = "%s"' % evolref
            where += ' AND [Client] IS NOT NULL'
            orderby = '[Client] ASC'
            cmd = 'SELECT DISTINCT [Client] FROM GDE_FEE_Details WHERE %s ORDER BY %s' % (where, orderby)
            cursor.execute(cmd)
            rows = cursor.fetchall()
            return [row[0] for row in rows]
    
    def get_programs(self, evolref, customer):
        with SqlServerConnection() as cursor:
            where = '[No DE] = "%s"' % evolref
            where += ' AND [Programme] IS NOT NULL'
            if customer:
                where += ' AND [Client] = "%s"' % customer.replace('"','""')
            orderby = '[Programme] ASC'
            cmd = 'SELECT DISTINCT [Programme] FROM GDE_FEE_Details WHERE %s ORDER BY %s' % (where, orderby)
            cursor.execute(cmd)
            rows = cursor.fetchall()
            return [row[0] for row in rows]
    
    def get_applications(self, evolref, customer, program):
        with SqlServerConnection() as cursor:
            where = '[No DE] = "%s"' % evolref
            where += ' AND [Application] IS NOT NULL'
            if customer:
                where += ' AND [Client] = "%s"' % customer.replace('"','""')
            if program:
                where += ' AND [Programme] = "%s"' % program.replace('"','""')
            orderby = '[Application] ASC'
            cmd = 'SELECT DISTINCT [Application] FROM GDE_FEE_Details WHERE %s ORDER BY %s' % (where, orderby)
            cursor.execute(cmd)
            rows = cursor.fetchall()
            return [row[0] for row in rows]
    
    def get_items(self, evolref, customer, program, application):
        with SqlServerConnection() as cursor:
            where = '[No DE] = "%s"' % evolref
            if customer:
                where += ' AND [Client] = "%s"' % customer.replace('"','""')
            if program:
                where += ' AND [Programme] = "%s"' % program.replace('"','""')
            if application:
                where += ' AND [Application] = "%s"' % application.replace('"','""')
            orderby = '[Article] ASC'
            cmd = 'SELECT DISTINCT [Article],[PN_produit_fini],[Amdt] FROM GDE_FEE_Details WHERE %s ORDER BY %s' % (where, orderby)
            cursor.execute(cmd)
            rows = cursor.fetchall()
            for row in rows:
                yield row
