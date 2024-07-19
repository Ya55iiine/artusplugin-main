# -*- coding: utf-8 -*-
#
# Copyright (C) 2016 Artus
# All rights reserved.
#
# Author: Michel Guillot <michel.guillot@meggitt.com>

""" Provides support for buildbot integration. """

# Trac
from trac.core import Component, implements, TracError
from trac.web import IRequestHandler

# Standard lib
from urllib.parse import unquote
import os
import re
import stat
import threading
from grp import getgrnam  # @UnresolvedImport

# Same package
from artusplugin import _
from artusplugin.util import lineno, unix_cmd_apply, parse_query_string, OrderedSet
from artusplugin.model import Tag


class BuildBotModule(Component):
    """Provides support for buildbot integration."""

    buildbot_projects = '/home/buildbot/projects'

    implements(IRequestHandler)

    def __init__(self):
        Component.__init__(self)
        self._lock = threading.Lock()

    # IRequestHandler methods

    def match_request(self, req):
        """ BuildBot requests handling """
        match = re.match(r'^/build$', req.path_info)
        if match:
            return True

    def process_request(self, req):
        """ BuildBot requests handling """
        if req.path_info.startswith('/build'):
            query_string = unquote(req.environ['QUERY_STRING'])
            args = parse_query_string(query_string)

            # CSCI name
            csci_name = args.get('csci_name', '')

            # EOC name
            eoc_name = args.get('eoc_name', '')

            if eoc_name:
                tag_url = Tag(self.env, eoc_name).tag_url
            else:
                tag_url = ''

            # Build type
            build_type = args.get('build_type', '')

            if 'makefile_url' in args:
                # Display the waterfall page showing all builds
                makefile_url = args['makefile_url']

            elif 'status_url' in args:
                # Display the status of a particular build
                status_url = args['status_url']

                # CI, build type & build no
                match = re.match(r'%s://(?:[^/]+)/tracs/buildbot/builders/([^/]+)_(prod|check)/builds/(\d+)' % req.scheme, status_url)

                if match:
                    # data is prepared for display
                    data = {}
                    data.update({'title': match.group(1) + _(' type ') + match.group(2) + _(' build no ') + match.group(3),
                                 'url': status_url, 'category': args['category'], 'builder_name': args['builder_name'], 'eoc_name': args['eoc_name'], 'eoc_tag_url': args.get('eoc_tag_url', '')})

                    if tag_url is None or build_type == 'check':
                        # Activated Build buttons
                        data.update({'csci_name': csci_name, 'username': args['username']})

                    return 'build.html', data, None
                else:
                    raise TracError("An error has occured in %s: incorrect 'status_url' parameter: <%s>. Please contact your trac administrator." % (lineno(), status_url))

            elif 'eoc_url' in args:
                # Browse the builded EOC
                eoc_url = args['eoc_url']
                # data is prepared for display
                data = {}
                data.update({'title': 'Builded EOC', 'url': eoc_url})
                return 'build.html', data, None

            else:
                raise TracError("'makefile_url' or 'status_url' or 'eoc_url' were not found in the query string (%s)." % lineno())

            url_parts = self.makefile_url_parts(req, makefile_url, build_type)

            if not url_parts:
                raise TracError("An error has occured in %s: incorrect 'makefile_url' parameter: <%s>. Please contact your trac administrator." % (lineno(), makefile_url))

            # New build ?
            buildbot_homedir = os.path.expanduser("~buildbot")
            builddir_path = buildbot_homedir + "/buildmaster/builddir.conf"

            # critical section
            self._lock.acquire()
            reconfig = False
            try:
                builddir_url = "%(project)s/%(skill)s%(variable)s/%(component)s/%(build_dir)s/%(build_type)s/%(buildbot_build_dir)s/%(build_csci)s/" % url_parts
                if url_parts['makefile_dir']:
                    builddir_url += "%(makefile_dir)s/" % url_parts
                builddir_url += "%(makefile_name)s" % url_parts

                lines = [line.rstrip('\n') for line in open(builddir_path)]
                line_sets = []
                if builddir_url in lines:
                    lines.remove(builddir_url)
                else:
                    reconfig = True
                # builddir url put ahead of list to be sure to keep it
                # as it is what is requested right now therefore
                # it is legitimate
                lines.insert(0, builddir_url)
                for line in lines:
                    line_parts = self.builddir_url_parts(line)
                    line_set = OrderedSet([line_parts['project'],
                                           line_parts['build_type'],
                                           line_parts['component']])
                    if line_set in line_sets:
                        # filter out conflicting line
                        # even if it is legitimate as
                        # it is not what is requested
                        # right now
                        lines.remove(line)
                        reconfig = True
                    line_sets.append(line_set)

                if reconfig:
                    # sort lines
                    lines.sort()
                    # Rewrites filtered out, appended and sorted builddir.conf file
                    open(builddir_path, 'w').writelines([line + '\n' for line in lines])
                    # Build of the required buildbot path
                    path = '/home/buildbot/projects/%(project)s' % url_parts
                    if not os.access(path, os.F_OK):
                        os.mkdir(path)
                    path += '/' + url_parts['skill']
                    if not os.access(path, os.F_OK):
                        os.mkdir(path)
                    for directory in url_parts['variable'].split('/'):
                        if directory == '':
                            continue
                        path += '/' + directory
                        if not os.access(path, os.F_OK):
                            os.mkdir(path)
                    path += '/' + url_parts['component']
                    if not os.access(path, os.F_OK):
                        os.mkdir(path)
                    path += '/' + url_parts['build_dir']
                    if not os.access(path, os.F_OK):
                        os.mkdir(path)
                    path += '/' + build_type
                    if not os.access(path, os.F_OK):
                        os.mkdir(path)
                    if os.stat(path).st_uid == os.getuid():
                        os.chmod(path, stat.S_IRWXU | stat.S_IRWXG | stat.S_IROTH | stat.S_IXOTH)
                        os.chown(path, -1, getgrnam('buildbot')[2])  # group => buildbot (apache is a member of that group)

                    # Buildbot reconfig
                    unix_cmd_list = ['sudo /usr/bin/buildbot reconfig ' + buildbot_homedir + '/buildmaster']

                    # Effective application of the list of commands
                    unix_cmd_apply(self.env, unix_cmd_list, lineno())

            finally:
                self._lock.release()

            # Builder name
            builder_name = '%(component)s_%(build_type)s' % url_parts
            if (url_parts['project'] != 'SB' and url_parts['project'].endswith('SB')) or url_parts['project'].endswith('FF') or url_parts['project'].endswith('_SW'):
                builder_name = builder_name.replace(url_parts['my_project'], url_parts['project'], 1)

            # Category name
            reg_exp = r'(%s_.+?)_[\w-]+(_%s)' % (url_parts['project'], build_type)
            match = re.match(reg_exp, builder_name)
            if match:
                category = match.group(1) + match.group(2)
            else:
                raise TracError("An error has occured in %s. "
                                "Builder name <%s> does not match regular expression <%s>. "
                                "Please contact your trac administrator." % (lineno(), builder_name, reg_exp))

            # url suitable for redirection to the twisted web reverse proxy used for distribution
            url = '%(scheme)s://%(host)s/tracs/buildbot/waterfall' % {'scheme': req.scheme, 'host': url_parts['host']}

            # data is prepared for display
            data = {}
            data.update({'title': _('Type ') + build_type + _(' build'),
                         'url': url,
                         'category': category,
                         'builder_name': builder_name,
                         'eoc_name': eoc_name,
                         'eoc_tag_url': args.get('eoc_tag_url', '')})

            if tag_url is None or build_type == 'check':
                # Activated Build buttons
                data.update({'csci_name': csci_name, 'username': req.authname})

            return 'build.html', data, None

    def makefile_url_parts(self, req, makefile_url, build_type):
        url_parts = {}

        match = re.match(r'^%s://(?P<host>[^/]+)/'
                         r'(?P<project>(?P<my_project>.+?)(?:SB|FF|_SW)?)/'
                         r'(?:B\d+/)?'
                         r'(?P<skill>[^/]+)'
                         r'(?P<variable>.*?)/'
                         r'(?P<component>(?P=my_project)_[^/]+)/'
                         r'(?P<build_dir>[^/]+)/'
                         r'(?P<build_csci>(?P=my_project)_[^/]+)/'
                         r'(?:(?P<makefile_dir>[^/]+)/)?'
                         r'(?P<makefile_name>[^/]+)$' % req.scheme,
                         makefile_url)
        if match:
            url_parts['host'] = match.group('host')
            url_parts['project'] = match.group('project')
            url_parts['my_project'] = match.group('my_project')
            url_parts['skill'] = match.group('skill')
            url_parts['variable'] = match.group('variable')
            url_parts['component'] = match.group('component')
            url_parts['build_dir'] = match.group('build_dir')
            url_parts['build_type'] = build_type
            url_parts['buildbot_build_dir'] = 'build'
            url_parts['build_csci'] = match.group('build_csci')
            url_parts['makefile_dir'] = match.group('makefile_dir')
            url_parts['makefile_name'] = match.group('makefile_name')

        return url_parts

    @staticmethod
    def builddir_url_parts(builddir_url):
        url_parts = {}

        match = re.match(r'^(?P<project>(?P<my_project>.+?)(?:SB|FF|_SW)?)/'
                         r'(?P<skill>[^/]+)'
                         r'(?P<variable>.*?)/'
                         r'(?P<component>(?P=my_project)_[^/]+)/'
                         r'(?P<build_dir>[^/]+)/'
                         r'(?P<build_type>[^/]+)/'
                         r'(?P<buildbot_build_dir>[^/]+)/'
                         r'(?P<build_csci>(?P=my_project)_[^/]+)/'
                         r'(?:(?P<makefile_dir>[^/]+)/)?'
                         r'(?P<makefile_name>[^/]+)$',
                         builddir_url)

        if match:
            url_parts['project'] = match.group('project')
            url_parts['my_project'] = match.group('my_project')
            url_parts['skill'] = match.group('skill')
            url_parts['variable'] = match.group('variable')
            url_parts['component'] = match.group('component')
            url_parts['build_dir'] = match.group('build_dir')
            url_parts['build_type'] = match.group('build_type')
            url_parts['buildbot_build_dir'] = match.group('buildbot_build_dir')
            url_parts['build_csci'] = match.group('build_csci')
            url_parts['makefile_dir'] = match.group('makefile_dir')
            url_parts['makefile_name'] = match.group('makefile_name')

        return url_parts
