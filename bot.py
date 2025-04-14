# Echochamber
#   - Group chats for BlueSky
#
# (C) 2025 All For Eco AB, Jan Lindblad
# See LICENSE for license conditions

import os, time, logging
from threading import Thread, get_ident
from atproto import Client, models, IdResolver, client_utils
import atproto_client.exceptions
import atproto_client, atproto_server
from msgs import ShutdownMsg, StartupMsg

# FIXME
# patched ...python.../site-packages/atproto_client/models/chat/bsky/convo/get_log.py
# class Response(base.ResponseModelBase):
# ...
#    logs: t.List[
#        te.Annotated[
#            t.Union[
# ...
#                 'models.ChatBskyConvoDefs.LogReadMessage', # FIXME JANL

log = logging.getLogger("echochamber.bot")

class BlueSkyBot(Thread):
    running_bots = {}

    @staticmethod
    def get_bot_count():
        return len(BlueSkyBot.running_bots)

    def __init__(self, queue, handle, username, password, hostname):
        super().__init__()
        self.queue = queue
        self.username = username
        self.password = password
        self.hostname = hostname
        self.handle   = handle
        self.stop = False
        self.convo = {}
        self.followers = {}
        self.communicated_followers = {}
        self.muted_users = self.read_muted_users()
        self.recently_processed_messages = set() # FIXME occasionally prune this set
        self.connect()
        log.info(f"BlueSkyBot connected to {self.hostname} with handle {self.handle} did {self.did}")
        self.inform_about_followers()

    def connect(self):
        self.client = Client(self.hostname)
        self.client.login(
            self.username, 
            self.password
        )
        self.dm_client = self.client.with_bsky_chat_proxy()
        self.id_resolver = IdResolver()
        self.did = self.id_resolver.handle.resolve(self.handle)

    def start(self):
        self.thread = Thread(target=BlueSkyBot.run, args=[self])
        self.thread.daemon = True
        self.thread.start()
        already_running_bot = BlueSkyBot.running_bots.get(self.handle)
        if already_running_bot:
            already_running_bot.stop = True
        BlueSkyBot.running_bots[self.handle] = self

    @staticmethod
    def run(self):
        log.info(f"BlueSkyBot {self.handle}:{get_ident()} starting")
        self.listen_to_users()
        log.info(f"BlueSkyBot {self.handle}:{get_ident()} stopping")

    def listen_to_users(self):
        log.info(f"BlueSkyBot {self.handle}:{get_ident()} listening...")
        log_cursor = None
        bsky_retries = 0
        while not self.stop and bsky_retries < 10:
            try:
                dm_logs = self.dm_client.chat.bsky.convo.get_log({"cursor":log_cursor})
            except atproto_client.exceptions.InvokeTimeoutError: 
                log.warning(f"Unable to reach BSKY")
                time.sleep(15)
                continue
            except atproto_server.exceptions.InvalidTokenError as e:
                log_cursor = None # Old cursor not valid with new connection
                log.info("Invalid token, renewing connection")
                time.sleep(2)
                self.connect()
                continue
            except atproto_client.exceptions.BadRequestError as e:
                if e.response.content.error == "ExpiredToken":
                    log.info("Expired token, renewing connection")
                    time.sleep(2)
                    self.connect()
                else:
                    raise
            except atproto_client.exceptions.NetworkError as e:
                log.info("Network error, renewing connection")
                time.sleep(60)
                self.connect()
                continue
            except atproto_client.exceptions.ModelError as e:
                log.exception(f"Pydantic validation exception") #, exc_info=e)
                continue
            except Exception as e:
                log.exception(f"Other bsky exception", exc_info=e)
                if bsky_retries >= 3:
                    log.error(f"Unable to get message log, {bsky_retries} retries")
                    raise Exception("BSKY Unable to get message log")
                bsky_retries += 1
                log_cursor = None # Max cursor life is about one hour
                log.info("Renewing cursor")
                time.sleep(2)
                continue
            bsky_retries = 0
            log_cursor = dm_logs.cursor
            for event in dm_logs.logs:
                if isinstance(event, atproto_client.models.chat.bsky.convo.defs.LogBeginConvo):
                    # When someone starts a conversation
                    log.info(f"Received LogBeginConvo event {event}")
                    #log.info(f"Event details {event.__dict__}")
                    self.update_followers()
                    continue
                elif isinstance(event, atproto_client.models.chat.bsky.convo.defs.LogLeaveConvo):
                    # When someone leaves a conversation? Never seen
                    log.info(f"Received LogLeaveConvo event {event}")
                    # Event details {'convo_id': '3lirhhlpv5a2h', 'rev': '222222335esrd', 'py_type': 'chat.bsky.convo.defs#logLeaveConvo'}
                    #log.info(f"Event details {event.__dict__}")
                    self.update_followers()
                    continue
                elif isinstance(event, atproto_client.models.chat.bsky.convo.defs.LogAcceptConvo):
                    # When someone follows?
                    continue
                elif not hasattr(event, "message"):
                    log.debug(f"Received and ignored event: {event} {type(event)}")
                    continue
                if event.message.sender.did == self.did:
                    log.debug(f"Echo of own message {event.message.sender.did}: {event.message.text}")
                    continue
                if event.message.text.strip().startswith("/"):
                    log.info(f"Admin command from {event.message.sender.did}")
                else:
                    log.info(f"Message from {event.message.sender.did}: {event.message.text}")
                # atproto_client.models.chat.bsky.convo.defs.MessageView
                if event.message.id in self.recently_processed_messages:
                    log.info(f"Duplicate message {event.message.id}, ignoring")
                    continue
                self.recently_processed_messages.add(event.message.id)
                if not self.handle_command(event.message.sender.did, event.message.text):
                    log.info(f"Facet details {event.message.facets}")
                    self.update_followers()
                    self.tell_room_about_follower_changes()
                    self.tell_room_users(event.message.sender.did, event.message)
            # Polling interval
            time.sleep(15)
        log.info(f"BlueSkyBot {self.handle} Terminating.")

    def handle_command(self, sender_did, text):
        try:
            if not text.strip().startswith("/"):
                return False
            words = text.strip().split(" ")
            if   words[0] == "/help":     self.handle_help_command(sender_did, words[1:])
            elif words[0] == "/shutdown": self.handle_shutdown_command(sender_did, words[1:])
            elif words[0] == "/startup":  self.handle_startup_command(sender_did, words[1:])
            elif words[0] == "/who":      self.handle_who_command(sender_did)
            elif words[0] == "/who-is":   self.handle_whois_command(sender_did, words[1:])
            elif words[0] == "/mute":     self.handle_mute_command(sender_did, words[1:])
            elif words[0] == "/muted":    self.handle_muted_command(sender_did)
            else:
                self.tell_one_user(sender_did, "Admin command not understood.")
            return True
        except Exception as e:
            log.error(f"Admin command failed, {e}")
            self.tell_one_user(sender_did, f"Admin command failed.")
            return True

    def handle_help_command(self, sender_did, words):
        if not words:
            self.tell_one_user(
                sender_did, 
                f"""Admin commands:
/help [<command>]
        List admin commands,
        or show help for a specific command
/who
        List users in this Echochamber
/who-is <user>  
        Show details about <user>
/mute <app_password> <did>
        Mute user with id <did>
/muted
        List muted users
/shutdown <app_password>
        Shut down this Echochamber
/startup <handle> <username> <app_password> [<hostname>]
        Startup new Echochamber (on same bsky host, if not specified)""")
            return
        cmd = words[0].strip()
        if cmd.startswith('/'): cmd = cmd[1:]
        resp = f"Unknown command, '{cmd}'."
        if cmd == "help": 
            resp = """/help   List the admin commands.\n/help <command>   Explain the specific admin command."""
        elif cmd == "who":
            resp = """/who   List the users that will receive the messages you type in this Echochamber chat."""
        elif cmd == "who-is":
            resp = """/who-is <user>   Show details about the user or users that match the user name or id given."""
        elif cmd == "mute":
            resp = """/mute <app_password> <did>   Permanently expel the user with the given id from this and all other Echocambers hosted by this server. To do this, the app_password for thie Echochamber needs to be provided."""
        elif cmd == "muted":
            resp = """/muted   List muted users. Muted users are expelled and not able to communicate with the Echochamber."""
        elif cmd == "shutdown":
            resp = """/shutdown <app_password>   Shutdown this Echochamber. The BlueSky account will remain, but no echoing will happen. To do this, the app_password for thie Echochamber needs to be provided."""
        elif cmd == "startup":
            resp = """/startup <handle> <username> <app_password> [<hostname>]   Start a new Echochamber. The BlueSky account needs to already exist, and its handle, login username and app_password have to be specified. If hostname is given, that BlueSky host will be contacted. If not, the same BlueSky host as this Echochamber is running on will be assumed."""
        else:
            pass
        self.tell_one_user(sender_did, resp)

    def handle_shutdown_command(self, sender_did, words):
        if words and words[0] == self.password:
            self.tell_one_user(
                sender_did, 
                f"Echochamber: Shutdown accepted"
            )
            del BlueSkyBot.running_bots[self.handle]
            self.queue.put(ShutdownMsg(self.handle))
            self.stop = True
        else:
            self.tell_one_user(
                sender_did, 
                f"Echochamber: Shutdown not authorized"
            )

    def handle_startup_command(self, sender_did, words):
        # /startup <handle> <username> <app_password> [<hostname>]
        if len(words) < 3:
            self.tell_one_user(
                sender_did, 
                f"Echochamber: Startup command missing information, Echochamber not started"
            )
        handle = words.pop(0)
        username = words.pop(0)
        app_password = words.pop(0)
        if words:
            hostname = words.pop(0)
        else:
            hostname = self.hostname
        self.queue.put(StartupMsg(handle, username, app_password, hostname))
        self.tell_one_user(
            sender_did, 
            f"Echochamber: Startup of Echochamber {handle} requested"
        )

    def handle_who_command(self, sender_did):
        self.update_followers()
        other_follower_names = ", ".join(
            [self.get_follower_name(follower_did)
                for follower_did in self.followers.keys() 
                if follower_did != sender_did]
        )
        if len(self.followers) >= 3:
            self.tell_one_user(
                sender_did, 
                f"Echochamber: There are {len(self.followers)-1} other members here: {other_follower_names}"
            )
        elif len(self.followers) == 2:
            self.tell_one_user(
                sender_did, 
                f"Echochamber: There is one other member here: {other_follower_names}"
            )
        else:
            self.tell_one_user(
                sender_did, 
                f"Echochamber: There are no other members here."
            )

    def handle_whois_command(self, sender_did, words):
        count_matching_users = 0
        for follower in self.followers.values():
            for word in words:
                if  word in follower.did or \
                    word in follower.handle or \
                    word in follower.display_name:
                    count_matching_users += 1
                    self.show_user_details(follower, sender_did)
        if not count_matching_users:
            self.tell_one_user(sender_did, f"No matching users found.")

    def show_user_details(self, sender_did, *followers):
        for follower in followers:
            self.tell_one_user(
                sender_did, 
                f"{follower.display_name} ({follower.handle}) {follower.did}"
            )

    def handle_muted_command(self, sender_did):
        self.tell_one_user(sender_did, f"""Muted users: {", ".join(self.muted_users)}""")

    def handle_mute_command(self, sender_did, words):
        if not words:
            self.handle_muted_command(sender_did)
            return
        app_password = words[0]
        target_dids = words[1:]
        if app_password == self.password:
            for target_did in target_dids:
                self.mute_user(target_did, sender_did)
            self.handle_muted_command(sender_did)
        else:
            self.tell_one_user(
                sender_did, 
                f"Echochamber: Muting not authorized"
            )

    def tell_room_users(self, sender_did, rich_message):
        if sender_did in self.muted_users:
            log.info(f"Muted user {sender_did} is trying to post. Rejected.")
            return
        from_name = self.get_follower_name(sender_did)
        for member_did in self.followers:
            if member_did == sender_did:
                continue
            message_builder = client_utils.TextBuilder()
            message_builder.text(f"{from_name}: ")
            self.recompose(message_builder, rich_message)
            self.tell_one_user(member_did, message_builder)

    def get_follower_name(self, did):
        if did == self.did:
            return "Echochamber"
        return self.get_follower_names().get(did, f"Anonymous {did}")

    def get_follower_names(self, follower_dict = None):
        if not follower_dict:
            follower_dict = self.followers
        return {f.did: f.display_name if f.display_name else f.handle for f in follower_dict.values()}

    def update_followers(self):
        self.followers = {follower.did:follower for follower in self.list_followers()}

    def tell_room_about_follower_changes(self):        
        announce_text = ""
        new_follows = set(self.followers) - set(self.communicated_followers)
        new_unfollows = set(self.communicated_followers) - set(self.followers)
        if new_follows:
            announce_text += ", ".join([self.get_follower_name(did) for did in self.followers.keys()]) + " joined the conversation. "
        if new_unfollows:
            announce_text += ", ".join([self.get_follower_name(did) for did in self.followers.keys()]) + " left."
        if self.communicated_followers and (new_follows or new_unfollows):
            # If there are no communicated_followers, the server was likely restarted
            # No need to mention anything
            self.tell_room_users(self.did, announce_text)
        if new_follows or new_unfollows:
            self.communicated_followers = self.followers.copy()

    def inform_about_followers(self):
        self.update_followers()
        if not self.followers:
            log.info("No followers")
            return
        log.info(f"BlueSkyBot {self.handle} has followers:")
        for n, did in enumerate(self.followers.keys()):
            log.info(f"Follower #{n}: {did} {self.followers[did].display_name} ({self.followers[did].handle}) {self.followers[did]}")

    def get_muted_users_filename(self):
        datadir = os.environ.get("ECHOCHAMBER_DATADIR", ".")
        filename = f"{datadir}/muted_users.txt"
        return filename

    def read_muted_users(self):
        muted_users = set()
        filename = self.get_muted_users_filename()
        with open(filename, "r") as f:
            self.muted_users = []
            for didstr in f.readlines():
                did = didstr.strip()
                if did and did[0] != "#":
                    muted_users.add(did)
        log.info(f"""Muted users: {", ".join(muted_users)}""")
        return muted_users

    def mute_user(self, target_did, issuer_did):
        filename = self.get_muted_users_filename()
        self.muted_users.add(target_did)
        with open(filename, "a") as f:
            print(f"# User {issuer_did} muted {target_did} on {time.ctime()}\n{target_did}", file=f)
        log.info(f"{issuer_did} muted user {target_did}")

    def list_followers(self):
        cursor = 1
        while cursor:
            reply = self.client.app.bsky.graph.get_followers(params={
                "actor": self.handle,
                "cursor": cursor if cursor != 1 else None
            })

            batchdata = reply.followers
            for follower in batchdata:
                if follower.did not in self.muted_users:
                    yield follower
            cursor = reply.cursor

    def tell_one_user(self, user, message):
        if isinstance(message, str):
            message_text = message
            message_facets = None
        else:
            message_text = message.build_text()
            message_facets = message.build_facets()
        log.info(f"Telling {user} {message_text}")
        convo = self.get_user_convo(user)
        self.dm_client.chat.bsky.convo.send_message(
            models.ChatBskyConvoSendMessage.Data(
                convo_id=convo.id,
                message=models.ChatBskyConvoDefs.MessageInput(
                    text=message_text,
                    facets=message_facets,
                ),
            )
        )

    def get_user_convo(self, did):
        if did in self.convo:
            return self.convo[did]
        self.convo[did] = self.dm_client.chat.bsky.convo.get_convo_for_members(
            models.ChatBskyConvoGetConvoForMembers.Params(members=[self.did, did]),
        ).convo
        return self.convo[did]

    def recompose(self, message_builder, rich_message):
        log.info(f"Recompose {rich_message}")
        if isinstance(rich_message, str):
            message_builder.text(rich_message)
            return

        # facets = [Main(features=[Link(uri='https://8.8.8.8/foo', py_type='app.bsky.richtext.facet#link')])]
        byte_str  = bytearray(rich_message.text, encoding="utf-8")
        byte_offs = 0
        if rich_message.facets:
            for i, fac in enumerate(rich_message.facets):
                log.info(f"Facet {i}: {fac}")
                if isinstance(fac, atproto_client.models.AppBskyRichtextFacet.Main):
                    if fac.index.byte_start > byte_offs:
                        text_only_slice = byte_str[byte_offs:fac.index.byte_start].decode("utf-8")
                        log.info(f"  Text: {text_only_slice}")
                        message_builder.text(text_only_slice)
                        byte_offs = fac.index.byte_end
                    substr = byte_str[fac.index.byte_start:fac.index.byte_end].decode("utf-8")
                    for i, feat in enumerate(fac.features):
                        byte_offs = fac.index.byte_end
                        if isinstance(feat, atproto_client.models.AppBskyRichtextFacet.Link):
                            log.info(f"  Link: {feat} {feat.uri}")
                            message_builder.link(substr, feat.uri)
                        elif isinstance(feat, atproto_client.models.AppBskyRichtextFacet.Mention):
                            log.info(f"  Mention: {feat} {feat.did}")
                            message_builder.mention(substr, feat.did)
                        elif isinstance(feat, atproto_client.models.AppBskyRichtextFacet.Tag):
                            log.info(f"  Tag: {feat} {feat.tag}")
                            message_builder.tag(str(substr), feat.tag)
                        else:
                            log.warning(f"Feature type unknown, ignored {i}: {fac} {feat}")
                else:
                    log.warning(f"Facet type unknown, ignored {i}: {fac}")
        if len(rich_message.text) > byte_offs:
            text_only_slice = byte_str[byte_offs:].decode("utf-8")
            log.info(f"  Final Text: {text_only_slice}")
            message_builder.text(text_only_slice)
