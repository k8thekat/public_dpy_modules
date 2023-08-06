# Reddit Image Scraper cog
import os
import logging
from typing_extensions import Union
from datetime import datetime, timezone, timedelta
from configparser import ConfigParser
from pathlib import Path
import pytz
import json
import hashlib
import sys
import tzlocal
import time

import urllib.request
import urllib.error

from fake_useragent import UserAgent
import praw
from praw.models import Subreddit

import utils.asqlite as asqlite

import discord
from discord.ext import commands, tasks

DB_FILENAME = "reddit_scrape.sqlite"

SUBREDDIT_SETUP_SQL = """
CREATE TABLE IF NOT EXISTS subreddit (
    id INT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    webhook_id INT,
    FOREIGN KEY (webhook_id) references webhook(id)
)"""

WEBHOOK_SETUP_SQL = """
CREATE TABLE IF NOT EXISTS webhook (
    id INT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    url TEXT NOT NULL UNIQUE
)"""


async def _get_subreddit(name: str):
    async with asqlite.connect(DB_FILENAME) as db:
        async with db.cursor() as cur:
            # await cur.execute("""SELECT lovers_id, partner_id FROM partners where s_time BETWEEN ? and ?""", value1, value2)
            await cur.execute("""SELECT id, name FROM subreddit where name = ?""", name)
            res = await cur.fetchone()
            return res if not None else None


async def _add_subreddit(name: str):
    res = await _get_subreddit(name=name)
    if res is None:
        return None
    else:
        async with asqlite.connect(DB_FILENAME) as db:
            async with db.cursor() as cur:
                await cur.execute("""INSERT INTO subreddit(name) VALUES(?) ON CONFLICT(name) DO NOTHING RETURNING *""", name)
                await db.commit()
                res = await cur.fetchone()
                return res if not None else None


async def _del_subreddit(name: str):
    res = await _get_subreddit(name=name)
    if res is None:
        return None
    else:
        async with asqlite.connect(DB_FILENAME) as db:
            async with db.cursor() as cur:
                await cur.execute("""DELETE FROM subreddit WHERE name = ?""", name)
                await db.commit()
                res = await cur.fetchone()
                return res if not None else None


async def _update_subreddit(name: str, id: int):
    # Check if the subreddit exists
    res = await _get_subreddit(name=name)
    if res is None:
        return None
    else:
        # Check if the webhook exists
        res = await _get_webhook(id)
        if res is None:
            return None
        else:
            webhook_id: int = res["ID"]

        async with asqlite.connect(DB_FILENAME) as db:
            async with db.cursor() as cur:
                await cur.execute("""UPDATE subreddit SET webhook_id WHERE name = ?  RETURNING *""", webhook_id, name)
                await db.commit()
                res = await cur.fetchone()
                return res if not None else None


async def _get_webhook(id: Union[None, int] = None, url: Union[None, str] = None, name: Union[None, str] = None):
    """returns `"name", "id", "url"` """
    async with asqlite.connect(DB_FILENAME) as db:
        async with db.cursor() as cur:
            if id is None and url is None and name is None:
                return None
            elif id is not None:
                await cur.execute("""SELECT name, id, url FROM webhook where id = ?""", id)
            elif url is not None:
                await cur.execute("""SELECT name, id, url FROM webhook where url = ?""", url)
            else:
                await cur.execute("""SELECT name, id url FROM webhook where name = ?""", name)
            res = await cur.fetchone()
            return res if not None else None


async def _add_webhook(name: str, url: str):
    res = await _get_webhook(url=url)
    if res is None:
        return None
    else:
        async with asqlite.connect(DB_FILENAME) as db:
            async with db.cursor() as cur:
                await cur.execute("""INSERT INTO webhook(name, url) VALUES(?, ?) ON CONFLICT(url) DO NOTHING RETURNING *""", name, url)
                await db.commit()
                res = await cur.fetchone()
                return res if not None else None


async def _del_webook(id: int):
    res = await _get_webhook(id=id)
    if res is None:
        return None
    else:
        async with asqlite.connect(DB_FILENAME) as db:
            async with db.cursor() as cur:
                await cur.execute("""UPDATE subreddit SET webhook_id WHERE webhook_id = ? RETURNING *""", id, None)
                await db.commit()
                await cur.execute("""DELETE FROM webhook WHERE webhook_id = ?""", id)
                await db.commit()
                return cur.get_cursor().rowcount


class Reddit_IS(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self._bot: commands.Bot = bot
        self._name: str = os.path.basename(__file__).title()
        self._logger = logging.getLogger()
        self._logger.info(f'**SUCCESS** Initializing {self._name} ')

        # PRAW information
        self._reddit_secret = ""
        self._reddit_client_id = ""
        self._reddit_username = ""
        self._reddit_password = ""

        self._json = 'reddit.json'
        self._url_list = []  # Recently sent URLs
        self._hash_list = []  # Recently hashed images
        self._url_prefixs: tuple[str, ...] = ("http://", "https://")

        # This is how many posts in each subreddit the script will look back.
        # By default the subreddit script looks at subreddits in `NEW` listing order.
        self._submission_limit = 30
        self._User_Agent = UserAgent().chrome

        # This forces the timezone to change based upon your OS for better readability in your prints()
        # This script uses `UTC` for functionality purposes.
        self._system_tz = tzlocal.get_localzone()
        # Need to use the string representation of the time zone for `pytz` eg. `America/Los_Angeles`
        self._pytz = pytz.timezone(str(self._system_tz))

        # Purely used to fill out the user_agent parameter of PRAW
        self._sysos = sys.platform.title()
        # self._user = reddit_token.reddit_username.title()

        # Feel free to change the User Name to suite you.
        self._user_name = "Kuma Bear of Reddit"

        # TODO - write function to pull DB names and generate subreddit list
        self._subreddits = []

        # TODO - setup task loop to check subreddits.
        # last_check = self.json_load()
        # self.check_loop(last_check=last_check)

    async def cog_load(self):
        """Load DB and settings from reddit_cog.ini"""
        async with asqlite.connect(DB_FILENAME) as db:
            await db.execute(SUBREDDIT_SETUP_SQL)
            await db.execute(WEBHOOK_SETUP_SQL)

    async def _ini_load(self):
        _setting_file: Path = Path("reddit_cog.ini")
        if _setting_file.is_file():
            settings = ConfigParser(converters={"list": lambda setting: [value.strip() for value in setting.split(",")]})
            settings.read(_setting_file.as_posix())
            self._reddit_secret: str = settings.get("PRAW", "reddit_secret")
            self._reddit_client_id: str = settings.get("PRAW", "reddit_client_id")
            self._reddit_username: str = settings.get("PRAW", "reddit_username")
            self._reddit_password: str = settings.get("PRAW", "reddit_password")

    def json_load(self):
        last_check: datetime = datetime.now(tz=timezone.utc)
        with open(self._json, "r") as jfile:
            data = json.load(jfile)
            print('Loaded our settings...')

        if 'last_check' in data:
            if data['last_check'] == 'None':
                last_check = datetime.now(tz=timezone.utc)
            else:
                last_check = datetime.fromtimestamp(
                    data['last_check'], tz=timezone.utc)
            print('Last Check... Done.')

        if 'url_list' in data:
            self._url_list = data['url_list']
            print('URL List... Done.')

        if 'hash_list' in data:
            self._hash_list = data['hash_list']
            print('Hash List... Done.')
        jfile.close()
        return last_check

    def json_save(self, last_check: datetime):
        # I generate an upper limit of the list based upon the subreddits times the submissin search limit; this allows for configuration changes and not having to scale the limit of the list
        limiter = (len(self._subreddits) * self._submission_limit) * 3

        if len(self._url_list) > limiter:
            print(f'Trimming down url list...')
            self._url_list = self._url_list[len(self._url_list) - limiter:]

        if len(self._hash_list) > limiter:
            print(f'Trimming down hash list...')
            self._hash_list = self._hash_list[len(self._hash_list) - limiter:]

        data = {
            "last_check": last_check.timestamp(),
            "url_list": self._url_list,
            "hash_list": self._hash_list
        }
        with open(self._json, "w") as jfile:
            json.dump(data, jfile)
            print('Saving our settings...')
            jfile.close()

    @tasks.loop()
    async def check_loop(self, last_check: datetime):
        delay = 30
        diff_time = timedelta(minutes=delay)
        while (1):
            cur_time = datetime.now(tz=timezone.utc)
            print(f'Checking the time...Cur_time: {cur_time.astimezone(self._pytz).ctime()} last_time: {last_check.astimezone(self._pytz).ctime()} diff_time: {(cur_time - diff_time).astimezone(self._pytz).ctime()}')

            if cur_time - diff_time >= last_check:
                print('Times up...checking subreddits')
                count = self.subreddit_media_handler(last_check=last_check)
                last_check = cur_time
                self.json_save(last_check=last_check)

                if count >= 1:
                    print(f'Finished Sending {str(count) + " Images" if count > 1 else str(count) + " Image"}')
            else:
                print(f'Sleeping for {delay*30} seconds or {delay*0.5} minutes')
                time.sleep(delay * 30)

    def subreddit_media_handler(self, last_check: datetime):
        """Iterates through the subReddits Submissions and sends media_metadata"""
        count = 0
        found_post = False
        img_url: Union[str, None] = None
        img_url_list: list[str] = []

        for sub in self._subreddits:
            cur_subreddit: Subreddit = self._reddit.subreddit(sub)
            # limit - controls how far back to go (true limit is 100 entries)
            for submission in cur_subreddit.new(limit=self._submission_limit):
                post_time: datetime = datetime.fromtimestamp(submission.created_utc, tz=timezone.utc)
                found_post = False
                print(f'Checking subreddit {sub} -> submission title: {submission.title} submission post_time: {post_time.astimezone(self._pytz).ctime()} last_check: {last_check.astimezone(self._pytz).ctime()}')

                if post_time >= last_check:  # The more recent time will be greater than..
                    # reset our img list
                    img_url_list = []
                    # Usually submissions with multiple images will be using this `attr`
                    if hasattr(submission, "media_metadata"):
                        # print('Found media_metadata')
                        # print(submission.media_metadata)

                        for key, img in submission.media_metadata.items():
                            # example {'status': 'valid', 'e': 'Image', 'm': 'image/jpg', 'p': [lists of random resolution images], 's': LN 105}
                            # This allows us to only get Images.
                            if "e" in img and img["e"] == 'Image':
                                # example 's': {'y': 2340, 'x': 1080, 'u': 'https://preview.redd.it/0u8xnxknijha1.jpg?width=1080&format=pjpg&auto=webp&v=enabled&s=04e505ade5889f6a5f559dacfad1190446607dc4'}, 'id': '0u8xnxknijha1'}
                                # img_url = img["s"]["u"]
                                img_url_list.append(img["s"]["u"])
                                continue

                            else:
                                continue

                    elif hasattr(submission, "url_overridden_by_dest"):
                        # print('Found url_overridden_by_dest')
                        # img_url = submission.url_overridden_by_dest
                        if submission.url_overridden_by_dest.startswith(self._url_prefixs):
                            img_url_list.append(submission.url_overridden_by_dest)
                        else:
                            continue

                    else:
                        continue

                    # if img_url != None:
                    if len(img_url_list) > 0:
                        for img_url in img_url_list:
                            if img_url in self._url_list:
                                continue

                            self._url_list.append(img_url)
                            status: bool = self.hash_process(img_url)

                            if status:
                                found_post = True
                                count += 1
                                self.webhook_send(content=f'**r/{sub}** ->  __[{submission.title}]({submission.url})__\n{img_url}\n')
                                time.sleep(1)  # Soft buffer delay between sends to prevent rate limiting.

            if found_post == False:
                print(f'No new Submissions in {sub} since {last_check.ctime()}')

        return count

    def hash_process(self, img_url: str) -> bool:
        """Checks the Hash of the supplied url against our hash list."""
        req = urllib.request.Request(url=img_url, headers={'User-Agent': str(self._User_Agent)})

        try:
            req_open = urllib.request.urlopen(req)
        except Exception as e:
            print(f'Unable to handle {img_url} with error: {e}')
            return False

        # This only gets "Images" and not "Videos" -> content_type() returns something like 'image/jpeg' or 'text/html'
        if 'image' in req_open.headers.get_content_type():
            my_hash = hashlib.sha256(req_open.read()).hexdigest()
            if my_hash not in self._hash_list:
                self._hash_list.append(my_hash)
                return True

            else:
                print('Found a duplicate hash...')
                return False
        else:  # Failed to find a 'image'
            print(f'URL: {img_url} is not an image -> {req_open.headers.get_content_type()}')
            return False

    def webhook_send(self, content: str):
        """Sends the Data to the Discord webhook"""
        data = {"content": content, "username": self._user if self._user_name == None else self._user_name}
        # TODO - Turn this into ASYNC
        result = requests.post(self._webhook_url, json=data)
        if 200 <= result.status_code < 300:
            print(f"Webhook sent {result.status_code}")
        else:
            print(f"Not sent with {result.status_code}, response:\n{result.json()}")
