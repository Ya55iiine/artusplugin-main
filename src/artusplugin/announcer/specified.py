from trac.core import Component, implements

from artusplugin.announcer.api import IAnnouncementAddressResolver
from artusplugin.announcer.api import IAnnouncementPreferenceProvider


class SpecifiedEmailResolver(Component):
    implements(IAnnouncementAddressResolver, IAnnouncementPreferenceProvider)

    def get_address_for_name(self, name, authenticated):
        db = self.env.get_db_cnx()
        cursor = db.cursor()

        cursor.execute("""
            SELECT value
              FROM session_attribute
             WHERE sid=%s
               AND authenticated=1
               AND name=%s
        """, (name,'announcer_specified_email'))

        result = cursor.fetchone()
        if result:
            return result[0]

        return None

    def set_address_for_name(self, email, name):
        db = self.env.get_db_cnx()

        try:
            cursor = db.cursor()
            cursor.execute("""
                INSERT INTO session_attribute
                (sid, authenticated, name, value)
                VALUES
                (%s, %s, %s, %s)
            """, (name, 1, 'announcer_specified_email', email))
            cursor.close()
        except Exception:
            cursor = db.cursor()
            cursor.execute("""
                UPDATE session_attribute
                SET value=%s
                WHERE sid=%s AND authenticated=%s AND name=%s
            """, (email, name, 1, 'announcer_specified_email'))
            cursor.close()

        db.commit()

    def remove_address_for_name(self, name):
        db = self.env.get_db_cnx()
        cursor = db.cursor()

        cursor.execute("""
            DELETE FROM session_attribute
             WHERE sid=%s
               AND authenticated=1
               AND name=%s
        """, (name,'announcer_specified_email'))

        cursor.close()
        db.commit()

    # IAnnouncementDistributor
    def get_announcement_preference_boxes(self, req):
        if req.authname != "anonymous":
            yield "emailaddress", "Announcement Email Address"

    def render_announcement_preference_box(self, req, panel):
        cfg = self.config
        sess = req.session

        if req.method == "POST":
            opt = req.args.get('specified_email', '')
            sess['announcer_specified_email'] = opt

        specified = sess.get('announcer_specified_email', '')

        data = dict(
            specified_email = specified,
        )

        return "prefs_announcer_emailaddress.html", data
