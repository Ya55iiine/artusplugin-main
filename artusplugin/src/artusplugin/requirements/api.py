# -*- coding: utf-8 -*-
#
# Copyright (C) 2016 Artus
# All rights reserved.
#
# Author: Michel Guillot <michel.guillot@meggitt.com>

""" Central functionality for the requirements package """

# Trac
from trac.core import Component, implements
from trac.ticket.api import ITicketChangeListener, ITicketManipulator

# Standard lib
import re

# Same package
from artusplugin.requirements.model import Requirement


class RequirementsSystem(Component):
    """Central functionality for the requirements package."""

    implements(ITicketChangeListener, ITicketManipulator)

    # ITicketManipulator methods

    def prepare_ticket(self, req, ticket, fields, actions):
        """Not currently called, but should be provided for future
        compatibility."""

        return

    def validate_ticket(self, req, ticket):
        """Validate a ticket after it's been populated from user input.

        Must return a list of `(field, message)` tuples, one for each problem
        detected. `field` can be `None` to indicate an overall problem with the
        ticket. Therefore, a return value of `[]` means everything is OK."""

        if ticket['requirements']:
            # Cleanup
            requirements = set([req for req in re.split('[^\w-]+', ticket['requirements']) if req])
            ticket['requirements'] = '\r\n'.join(requirements)

        return []

    # ITicketChangeListener methods

    def ticket_created(self, ticket):
        """Called when a ticket is created."""

        return

    def ticket_changed(self, ticket, comment, author, old_values):
        """Called when a ticket is modified.

        `old_values` is a dictionary containing the previous values of the
        fields that have changed.
        """

        if 'requirements' in old_values:
            # Update the requirement table
            db = self.env.get_db_cnx()
            ticket_id = str(ticket.id)
            for requirement in Requirement.select(self.env, ['ticket="' + ticket_id + '"'], db=db):
                requirement.delete(db=db)
            if ticket['requirements']:
                for requirement in ticket['requirements'].split('\r\n'):
                    req = Requirement(self.env, db=db)
                    req.requirement = requirement
                    req.ticket = ticket_id
                    req.insert(db=db)
            db.commit()

        return

    def ticket_deleted(self, ticket):
        """Called when a ticket is deleted."""

        return
