# -*- coding: utf-8 -*-
#
# Copyright (C) 2016 Artus
# All rights reserved.
#
# Author: Michel Guillot <michel.guillot@meggitt.com>

""" Provides support for requirements/ticket tabulation. """

from trac.core import Component, implements
from trac.web import IRequestFilter


class RequirementsModule(Component):
    """Provides support for requirements/ticket tabulation."""

    implements(IRequestFilter)

    def __init__(self):
        Component.__init__(self)

    # IRequestFilter methods

    def pre_process_request(self, req, handler):
        """The pre-processing done when a request is submitted to TRAC """

        return handler

    def post_process_request(self, req, template, data, content_type):
        """The post-processing done when a request is submitted to TRAC
           This is used for updating the requirement table after the requirements custom field has been updated by TRAC """

        if data is None:
            data = {}

        if template in ['ticket.html']:
            pass

        return (template, data, content_type)
