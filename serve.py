# Echo chamber
#   - Group chats for BlueSky
#
# (C) 2025 All For Eco AB, Jan Lindblad
# See LICENSE for license conditions

import logging, time, os, datetime
from threading import Thread
from queue import Queue
from dotenv import load_dotenv
from bot import BlueSkyBot
from msgs import ShutdownMsg, StartupMsg
from chambers import Chambers

# Load environment variables
load_dotenv()

log = logging.getLogger("echochamber.serve")

def setup_logging():
    logdir = os.environ.get("ECHOCHAMBER_LOGDIR", ".")
    logging.basicConfig(
        level=logging.INFO, 
        format='%(asctime)s:%(levelname)s:%(name)s:T%(thread)d: %(message)s',
        datefmt='%H:%M:%S',
        filename=f'{logdir}/all.log', 
        encoding='utf-8'
    )
    formatter = logging.Formatter(
        '%(asctime)s:%(levelname)s:%(name)s: %(message)s', 
        '%H:%M:%S')
    ecfh = logging.FileHandler(f'{logdir}/echochamber.log')
    ecfh.setFormatter(formatter)
    eclog = logging.getLogger("echochamber")
    eclog.addHandler(ecfh)
    eclog.setLevel(logging.INFO)

    Hourglass.start()

class Hourglass(Thread):
    def __init__(self):
        super().__init__()
        self.stop = False

    @staticmethod
    def start():
        hourglass = Hourglass()
        hourglass.start_thread()

    def start_thread(self):
        self.thread = Thread(target=Hourglass.run, args=[self])
        self.thread.daemon = True
        self.thread.start()

    @staticmethod
    def run(self):
        time.sleep(5)
        log.info(f"Hourglass {id(self)} starting")
        self.log_hours()
        log.info(f"Hourglass {id(self)} stopping")

    def log_hours(self):
        while not self.stop:
            now = datetime.datetime.now()
            past_hour = now.replace(minute=0).replace(second=0).replace(microsecond=0)
            next_hour = past_hour + datetime.timedelta(hours=1)
            till_next_hour = next_hour - now
            wakeup_interval = datetime.timedelta(minutes=5)
            if till_next_hour > wakeup_interval:
                time.sleep(wakeup_interval.total_seconds())
            else:
                time.sleep(till_next_hour.total_seconds()+0.25)
                log.info(f"### Hourglass turning {time.ctime()}")
                time.sleep(30)

def handle_admin_msgs(queue):
    while True:
        if not BlueSkyBot.get_bot_count():
            log.info("All echochambers have shutdown, terminating")
            break
        msg = queue.get()
        if isinstance(msg, ShutdownMsg):
            Chambers.delete(msg.handle)
        if isinstance(msg, StartupMsg):
            Chambers.create(msg)        

def main():
    setup_logging()
    log.info(f"\n\n### Echochamber starting on {time.ctime()}")

    super_admin_msg_queue = Queue()
    chambers = Chambers.get_definitions()
    for handle in chambers.keys():
        try:
            username = chambers[handle]['username']
            app_password = chambers[handle]['app_password']
            hostname = chambers[handle]['hostname']
            BlueSkyBot(super_admin_msg_queue, handle, username, app_password, hostname).start()
        except Exception as e:
            log.exception(f"Could not create echochamber {handle}, skipping", exc_info=e)

    log.info(f"### Echochamber listening on {time.ctime()}")
    print("Listening...")
    handle_admin_msgs(super_admin_msg_queue)
    log.info(f"### Echochamber terminating on {time.ctime()}")

if __name__ == "__main__":
    main()