from trac.db import Table, Column

schema_version = 6

# The 'subscriptions' db table has been dropped in favor of the new
# subscriber interface, that uses two other tables.
# TODO: We still need to create an upgrade script, that will port
# subscriptions from 'subscriptions' and 'session_attribute' db tables to
# 'subscription' and 'subscription_attribute'.

schema = [
    Table('subscription', key='id')[
        Column('id', auto_increment=True),
        Column('time', type='int64'),
        Column('changetime', type='int64'),
        Column('class'),
        Column('sid'),
        Column('authenticated', type='int'),
        Column('distributor'),
        Column('format'),
        Column('priority', type='int'),
        Column('adverb')
    ],
    Table('subscription_attribute', key='id')[
        Column('id', auto_increment=True),
        Column('sid'),
        Column('authenticated', type='int'),
        Column('class'),
        Column('realm'),
        Column('target')
    ]
]


def get_data(db):
    return (('system',
             ('name', 'value'),
             (('announcer_version', str(schema_version)),)),)