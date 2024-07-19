# -*- coding: utf-8 -*-
#
# Copyright (C) 2016 Artus
# All rights reserved.
#
# Author: Michel Guillot <michel.guillot@meggitt.com>

""" The code managing the database setup and upgrades. """

from trac.core import Component, implements
from trac.db.schema import Table, Column, Index
from trac.env import IEnvironmentSetupParticipant

__all__ = ['BuildBotSetup']

# Database version identifier for upgrades.
db_version = 2

# Database schema
schema = [
    # Builds
    Table('build', key=('builder', 'build_no'))[
        Column('builder'),
        Column('build_no', type='int'),
        Column('CSCI_tag'),
        Column('build_path'),
        Column('EOC_tag'),
        Column('completed', type='int'),
        Index(['CSCI_tag'])]
]

# Create tables


def to_sql(env, table):
    """ Convenience function to get the to_sql for the active connector."""
    from trac.db.api import DatabaseManager
    dm = env.components[DatabaseManager]
    dc = dm._get_connector()[0]
    return dc.to_sql(table)


def create_tables(cursor, env):
    """ Creates the tables as defined by schema.
    using the active database connector. """
    for table in schema:
        for stmt in to_sql(env, table):
            cursor.execute(stmt)
    cursor.execute("INSERT into system values ('buildbot_version', '2')")

# Upgrades


# Component that deals with database setup


class BuildBotSetup(Component):
    """Component that deals with database setup and upgrades."""

    implements(IEnvironmentSetupParticipant)

    def environment_created(self):
        """Called when a new Trac environment is created."""
        pass

    def environment_needs_upgrade(self, db):
        """Called when Trac checks whether the environment needs to be upgraded.
        Returns `True` if upgrade is needed, `False` otherwise."""
        cursor = db.cursor()
        return self._get_version(cursor) != db_version

    def upgrade_environment(self, db):
        """Actually perform an environment upgrade, but don't commit as
        that is done by the common upgrade procedure when all plugins are done."""
        cursor = db.cursor()
        if self._get_version(cursor) == 0:
            create_tables(cursor, self.env)
        else:
            # do upgrades here when we get to that...
            pass

    def _get_version(self, cursor):
        try:
            sql = "SELECT value FROM system WHERE name='buildbot_version'"
            self.log.debug(sql)
            cursor.execute(sql)
            for row in cursor:
                return int(row[0])
            return 0
        except Exception:
            return 0
