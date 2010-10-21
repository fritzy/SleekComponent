import sleekxmpp
from sleekxmpp.componentxmpp import ComponentXMPP
import sqlite3
from ConfigParser import ConfigParser
from sleekxmpp.xmlstream.jid import JID
from sleekxmpp.stanza.roster import Roster

import os
import logging


class UserNodes(object):
    
    def __init__(self, db, xmpp):
        self.db = db
        self.xmpp = xmpp
        self._user_nodes = {}

    def add(self, node):
        print "--------- adding %s" % node
        if node not in self._user_nodes:
            self._user_nodes[node] = Roster(self.db, self.xmpp, node)

    def __getitem__(self, key):
        if key not in self._user_nodes:
            self.add(key)
        return self._user_nodes[key]
    

class Roster(object):
    def __init__(self, db, xmpp, jid):
        self.db = db
        self.xmpp = xmpp
        self.jid = jid
        self._jids = {}

    def __getitem__(self, key):
        if key not in self._jids:
            self.addItem(key, save=True)
        return self._jids[key]
    
    def addItem(self, jid, afrom=False, ato=False, pending_in=False, pending_out=False, whitelisted=False, row_id=None, save=False):
        self._jids[jid] = RosterItem(self.db, self.xmpp, jid, self.jid, state={'from': afrom, 'to': ato, 'pending_in': pending_in, 'pending_out': pending_out, 'whitelisted': whitelisted}, row_id=row_id)
        if save:
            self._jids[jid].save()
    
class RosterItem(object):
    def __init__(self, db, xmpp, jid, component_jid=None, state=None, row_id=None):
        self.db = db
        self.xmpp = xmpp
        self.row_id = row_id
        self.jid = jid
        self.component_jid = component_jid or self.xmpp.jid
        self._state = state or {'from': False, 'to': False, 'pending_in': False, 'pending_out': False, 'whitelisted': False}
        self.last_status = None
        self.pull()
    
    def boolize(self, query):
        return str(query).lower() in ('true', '1', 'on', 'yes')
    
    def pull(self):
        c = self.db.cursor()
        c.execute("select id, afrom, ato, whitelisted, pending_out, pending_in from roster where component_jid=? and other_jid=?", (self.component_jid, self.jid))
        r = c.fetchall()
        if len(r) == 0:
            #self.save()
            return self._state
        r = r[0]
        self.row_id = int(r[0])
        self['from'] = r[1]
        self['to'] = r[2]
        self['whitelisted'] = r[3]
        self['pending_out'] = r[4]
        self['pending_in'] = r[5]
        c.close()
        return self._state

    def save(self):
        c = self.db.cursor()
        if self.row_id is None and True in self._state.values():
            c.execute("insert into roster (component_jid, other_jid, afrom, ato, whitelisted, pending_out, pending_in) values (?,?,?,?,?,?,?)", (self.component_jid, self.jid, int(self['from']), int(self['to']), int(self['whitelisted']), int(self['pending_out']), int(self['pending_in'])))
            self.row_id = c.lastrowid
        else:
            c.execute("update roster set afrom=?, ato=?, whitelisted=?, pending_out=?, pending_in=? where id=?", (int(self['from']), int(self['to']), int(self['whitelisted']), int(self['pending_out']), int(self['pending_in']), self.row_id))
        self.db.commit()
        c.close()
    
    def __getitem__(self, key):
        if key in self._state:
            return self._state[key]
        else:
            raise KeyError

    def __setitem__(self, key, value):
        if key in self._state:
            self._state[key] = self.boolize(value)
        else:
            raise KeyError
    
    def remove(self):
        "Remove the jids subscription, inform it if it is subscribed, and unwhitelist it"
        if self['to']:
            self.sendPresence(pto=row[2], pfrom=row[1], ptype='unsubscribe')
            self['to'] = False
        self['whitelisted'] = False
        self.save()
    
    def subscribe(self):
        p = self.xmpp.Presence()
        p['to'] = self.jid
        p['from'] = self.component_jid
        p['type'] = 'subscribe'
        self['pending_out'] = True
        self.save()
        p.send()
    
    def authorize(self):
        self['from'] = True
        self['pending_in'] = False
        self.save()
        self._subscribed()
        self.sendLastPresence()
    
    def unauthorize(self):
        self['from'] = False
        self['pending_in'] = False
        self.save()
        self._unsubscribed()
        p = self.xmpp.Presence()
        p['to'] = self.jid
        p['from'] = self.component_jid
        p['type'] = 'unavailable'
        p.send()
    
    def _subscribed(self):
        p = self.xmpp.Presence()
        p['to'] = self.jid
        p['from'] = self.component_jid
        p['type'] = 'subscribed'
        p.send()
    
    def unsubscribe(self):
        p = self.xmpp.Presence()
        p['to'] = self.jid
        p['from'] = self.component_jid
        p['type'] = 'unsubscribe'
        #self['to'] = False #probably best to wait for the unsubscribed response
        self.save()
        p.send()
    
    def _unsubscribed(self):
        p = self.xmpp.Presence()
        p['to'] = self.jid
        p['from'] = self.component_jid
        p['type'] = 'unsubscribed'
        p.send()
    
    def presence(self, ptype='available', status=None):
        p = self.xmpp.Presence()
        p['to'] = self.jid
        p['from'] = self.component_jid
        p['type'] = ptype
        p['status'] = status
        self.last_status = p
        p.send()
    
    def sendLastPresence(self):
        if self.last_status is None:
            self.presence()
        else:
            self.last_status.send()
    
    def handleSubscribe(self, presence):
        """
        +------------------------------------------------------------------+
        |  EXISTING STATE          |  DELIVER?  |  NEW STATE               |
        +------------------------------------------------------------------+
        |  "None"                  |  yes       |  "None + Pending In"     |
        |  "None + Pending Out"    |  yes       |  "None + Pending Out/In" |
        |  "None + Pending In"     |  no        |  no state change         |
        |  "None + Pending Out/In" |  no        |  no state change         |
        |  "To"                    |  yes       |  "To + Pending In"       |
        |  "To + Pending In"       |  no        |  no state change         |
        |  "From"                  |  no *      |  no state change         |
        |  "From + Pending Out"    |  no *      |  no state change         |
        |  "Both"                  |  no *      |  no state change         |
        +------------------------------------------------------------------+
        """
        if not self['from']  and not self['pending_in']:
            self['pending_in'] = True
            self.xmpp.event('roster_subscription_request', presence)
        elif self['from']:
            self._subscribed()
        self.save()
    
    def handleSubscribed(self, presence):
        """
        +------------------------------------------------------------------+
        |  EXISTING STATE          |  DELIVER?  |  NEW STATE               |
        +------------------------------------------------------------------+
        |  "None"                  |  no        |  no state change         |
        |  "None + Pending Out"    |  yes       |  "To"                    |
        |  "None + Pending In"     |  no        |  no state change         |
        |  "None + Pending Out/In" |  yes       |  "To + Pending In"       |
        |  "To"                    |  no        |  no state change         |
        |  "To + Pending In"       |  no        |  no state change         |
        |  "From"                  |  no        |  no state change         |
        |  "From + Pending Out"    |  yes       |  "Both"                  |
        |  "Both"                  |  no        |  no state change         |
        +------------------------------------------------------------------+
        """
        logging.debug((self.jid, self.component_jid))
        if not self['to'] and self['pending_out']:
            self['pending_out'] = False
            self['to'] = True
            self.xmpp.event('roster_subscription_authorized', presence)
        self.save()
    
    def handleUnsubscribe(self, presence):
        """
        +------------------------------------------------------------------+
        |  EXISTING STATE          |  DELIVER?  |  NEW STATE               |
        +------------------------------------------------------------------+
        |  "None"                  |  no        |  no state change         |
        |  "None + Pending Out"    |  no        |  no state change         |
        |  "None + Pending In"     |  yes *     |  "None"                  |
        |  "None + Pending Out/In" |  yes *     |  "None + Pending Out"    |
        |  "To"                    |  no        |  no state change         |
        |  "To + Pending In"       |  yes *     |  "To"                    |
        |  "From"                  |  yes *     |  "None"                  |
        |  "From + Pending Out"    |  yes *     |  "None + Pending Out     |
        |  "Both"                  |  yes *     |  "To"                    |
        +------------------------------------------------------------------+
        """
        if not self['from']  and self['pending_in']:
            self['pending_in'] = False
            self._unsubscribed()
        elif self['from']:
            self['from'] = False
            self._unsubscribed()
            self.xmpp.event('roster_subscription_remove', presence)
        self.save()

    def handleUnsubscribed(self, presence):
        """
        +------------------------------------------------------------------+
        |  EXISTING STATE          |  DELIVER?  |  NEW STATE               |
        +------------------------------------------------------------------+
        |  "None"                  |  no        |  no state change         |
        |  "None + Pending Out"    |  yes       |  "None"                  |
        |  "None + Pending In"     |  no        |  no state change         |
        |  "None + Pending Out/In" |  yes       |  "None + Pending In"     |
        |  "To"                    |  yes       |  "None"                  |
        |  "To + Pending In"       |  yes       |  "None + Pending In"     |
        |  "From"                  |  no        |  no state change         |
        |  "From + Pending Out"    |  yes       |  "From"                  |
        |  "Both"                  |  yes       |  "From"                  |
        +------------------------------------------------------------------
        """
        if not self['to'] and self['pending_out']:
            self['pending_out'] = False
        elif self['to'] and not self['pending_out']:
            self['to'] = False
            self.xmpp.event('roster_subscription_removed', presence)
        self.save()
    
    def handleProbe(self, presence):
        if self['to']: self.sendLastPresence()
        if self['pending_out']: self.subscribe()
        if not self['to']: self._unsubscribed()

class SleekComponent(ComponentXMPP):
    def __init__(self, config_file=None, config_connection=None, config_presence=None, config_roster=None):
        # load the configuration
        self.config = {}
        if config_file is not None:
            cf = ConfigParser()
            cf.read(config_file)
            self.config['connection'] = {
                    'domain': cf.get('connection', 'domain'),
                    'secret': cf.get('connection', 'secret'),
                    'host': cf.get('connection', 'host'),
                    'port': cf.getint('connection', 'port'),
                    }
            self.config['presence'] = {
                    'probe_initially': cf.getboolean('presence', 'probe_initially'),
                    'bcast_initially': cf.getboolean('presence', 'bcast_initially'),
                    }
            self.config['roster'] = {
                    'dbfile': cf.get('roster', 'dbfile'),
                    }
        if config_connection is not None:
            self.config['connection'] = config_connection
        if config_presence is not None:
            self.config['presence'] = config_presence
        if config_roster is not None:
            self.config['roster'] = config_roster

        #initialize the component with connection info
        ComponentXMPP.__init__(self, self.config['connection']['domain'], self.config['connection']['secret'], self.config['connection']['host'], self.config['connection']['port'])

        self.registerPlugin('xep_0045') # MUC
        self.add_event_handler('session_start', self.handleComponentStart)
        self.add_event_handler('presence_subscribe', self.handlePresSubscribe)
        self.add_event_handler('presence_subscribed', self.handlePresSubscribed)
        self.add_event_handler('presence_unsubscribe', self.handlePresUnsubscribe)
        self.add_event_handler('presence_unsubscribed', self.handlePresUnsubscribed)
        self.add_event_handler('presence_probe', self.handlePresProbe)
        self.add_event_handler('roster_subscription_request', self.handleNewSubscription)
        self.add_event_handler('roster_subscription_removed', self.handleRemovedSubscription)
        self.add_event_handler('got_online', self.handleGotOnline)

        self.current_status = {'default': self.Presence()}

    def handleComponentStart(self, session):
        self.rosterdb = sqlite3.connect(self.config['roster']['dbfile'])
        self.rosteritems = UserNodes(self.rosterdb, self)

        #load roster

        c = self.rosterdb.cursor()
        c.execute("select component_jid, other_jid, afrom, ato, whitelisted, pending_out, pending_in,id from roster")
        for item in c:
            self.rosteritems[item[0]].addItem(item[1], afrom=bool(int(item[2])), ato=bool(int(item[3])), pending_in=bool(int(item[6])), pending_out=bool(int(item[5])), whitelisted=bool(int(item[4])), save=False, row_id=int(item[7]))
            #send probes
            if self.config['presence']['probe_initially'] and int(item[3]):
                self.rosteritems[item[0]][item[1]].presence('probe')
            #send initial presence
            if self.config['presence']['bcast_initially'] and int(item[2]):
                self.rosteritems[item[0]][item[1]].presence()
            if bool(int(item[6])):
                p=self.Presence()
                p['from'] = item[1]
                p['to'] = item[0]
                p['type'] = 'subscribe'
                self.event('roster_subscription_request', p)
            if bool(int(item[5])):
                self.rosteritems[item[0]][item[1]].subscribe()
        c.close()
    
    def handleNewSubscription(self, presence):
        #override this!
        #TODO only do this if whitelisted or accept all is true
        self.rosteritems[presence['to'].bare][presence['from'].bare].authorize()
        self.rosteritems[presence['to'].bare][presence['from'].bare].subscribe()
    
    def handleRemovedSubscription(self, presence):
        #decent default behavior -- unsubscribe from those that unsubscribe you
        self.rosteritems[presence['to'].bare][presence['from'].bare].unauthorize()
    
    def handlePresSubscribe(self, stanza):
        #routing
        self.rosteritems[stanza['to'].bare][stanza['from'].bare].handleSubscribe(stanza)

    def handlePresSubscribed(self, stanza):
        #routing
        self.rosteritems[stanza['to'].bare][stanza['from'].bare].handleSubscribed(stanza)

    def handlePresUnsubscribe(self, stanza):
        #routing
        self.rosteritems[stanza['to'].bare][stanza['from'].bare].handleUnsubscribe(stanza)

    def handlePresUnsubscribed(self, stanza):
        #routing
        self.rosteritems[stanza['to'].bare][stanza['from'].bare].handleUnsubscribed(stanza)
    
    def handlePresProbe(self, stanza):
        #routing
        self.rosteritems[stanza['to'].bare][stanza['from'].bare].handleProbe(stanza)
    
    def handleGotOnline(self, stanza):
        if not 'muc' in stanza.plugins:
            self.rosteritems[stanza['to'].bare][stanza['from'].bare].presence()
        #TODO check for pending_in and re-request


if __name__ == '__main__':
    logging.basicConfig(level=5, format='%(levelname)-8s %(message)s')
    c = SleekComponent('../config.ini')
    c.connect()
    c.process()


