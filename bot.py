# Echo chamber
#   - Group chats for BlueSky
#
# (C) 2025 All For Eco AB, Jan Lindblad
# See LICENSE for license conditions

import os, time, logging
from threading import Thread, get_ident
from atproto import Client, models, IdResolver
import atproto_client, atproto_server
from msgs import ShutdownMsg

log = logging.getLogger("echochamber.bot")

class BlueSkyBot(Thread):
    running_bots = {}

    def __init__(self, queue, username, password, hostname, handle):
        super().__init__()
        self.queue = queue
        self.username = username
        self.password = password
        self.hostname = hostname
        self.handle   = handle
        self.stop = False
        self.muted_users = []
        self.convo = {}
        self.follower_names = {}
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
            except:
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
                    self.update_follower_names()
                    continue
                elif isinstance(event, atproto_client.models.chat.bsky.convo.defs.LogLeaveConvo):
                    # When someone leaves a conversation? Never seen
                    log.info(f"Received LogLeaveConvo event {event}")
                    self.update_follower_names()
                    continue
                elif event.message.sender.did == self.did:
                    log.debug(f"Echo of own message {event.message.sender.did}: {event.message.text}")
                    continue
                log.info(f"Message from {event.message.sender.did}: {event.message.text}")
                if not self.handle_command(event.message.sender.did, event.message.text):
                    self.tell_room_users(event.message.sender.did, event.message.text)
            # Polling interval
            time.sleep(15)
    log.info("Terminating.")

    def handle_command(self, sender_did, text):
        if text.startswith("/help"):
            self.handle_help_command(sender_did)
            return True
        if text.startswith("/shutdown"):
            self.queue.put(ShutdownMsg())
            return True
        if text.startswith("/who"):
            self.update_follower_names()
            self.handle_who_command(sender_did)
            return True
        return False

    def handle_help_command(self, sender_did):
        self.tell_one_user(
            sender_did, 
            f"""Admin commands:
            /help      List admin commands
            /who       List users in this Echo chamber
            /shutdown  Shut down all Echo chambers managed by this process
            """
        )

    def handle_who_command(self, sender_did):
        other_follower_names = ", ".join(
            [self.follower_names[follower_did] 
                for follower_did in self.follower_names.keys() 
                if follower_did != sender_did]
        )
        if len(self.follower_names) >= 3:
            self.tell_one_user(
                sender_did, 
                f"There are {len(self.follower_names)-1} other members in this Echo chamber: {other_follower_names}"
            )
        elif len(self.follower_names) == 2:
            self.tell_one_user(
                sender_did, 
                f"There is one other member in this Echo chamber: {other_follower_names}"
            )
        else:
            self.tell_one_user(
                sender_did, 
                f"There are no other members in this Echo chamber."
            )

    def tell_room_users(self, sender_did, text):
        self.update_follower_names()
        from_name = self.follower_names.get(sender_did, f"Anonymous {sender_did}")
        for member_did in self.follower_names.keys():
            if member_did == sender_did:
                continue
            self.tell_one_user(member_did, f"{from_name}: {text}")

    def update_follower_names(self):
        followers = self.list_followers()
        self.follower_names = {f.did: f.display_name if f.display_name else f.handle for f in followers}

    def inform_about_followers(self):
        self.update_follower_names()
        if not self.follower_names:
            log.info("No followers")
            return
        log.info(f"BlueSkyBot {self.handle} has followers:")
        for n, did in enumerate(self.follower_names.keys()):
            log.info(f"Follower #{n}: {self.follower_names[did]}")

    def list_followers(self):
        cursor = 1
        while cursor:
            reply = self.client.app.bsky.graph.get_followers(params={
                "actor": self.handle,
                "cursor": cursor if cursor != 1 else None
            })

            batchdata = reply.followers
            for follower in batchdata:
                if follower not in self.muted_users:
                    yield follower
            cursor = reply.cursor

    def tell_one_user(self, user, text_message):
        log.info(f"Telling {user} {text_message}")
        convo = self.get_user_convo(user)
        self.dm_client.chat.bsky.convo.send_message(
            models.ChatBskyConvoSendMessage.Data(
                convo_id=convo.id,
                message=models.ChatBskyConvoDefs.MessageInput(
                    text=text_message,
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
