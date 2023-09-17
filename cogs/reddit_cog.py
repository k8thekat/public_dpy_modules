# Reddit Image Scraper cog
from gc import is_finalized
import os
import logging
import sqlite3
from typing import Union
from datetime import datetime, timezone, timedelta
from configparser import ConfigParser
from pathlib import Path
import pytz
import json
import hashlib
import sys
from requests import Request
import tzlocal
import time
import aiohttp
from aiohttp import ClientResponse

import urllib.request
import urllib.error

from fake_useragent import UserAgent
import praw
from praw.models import Subreddit

import utils.asqlite as asqlite
from sqlite3 import Row

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
    """returns `id, name, webhook_id`"""
    async with asqlite.connect(DB_FILENAME) as db:
        async with db.cursor() as cur:
            # await cur.execute("""SELECT lovers_id, partner_id FROM partners where s_time BETWEEN ? and ?""", value1, value2)
            await cur.execute("""SELECT id, name, webhook_id FROM subreddit where name = ?""", name)
            res = await cur.fetchone()
            return res if not None else None


async def _add_subreddit(name: str):
    res = await _get_subreddit(name=name)
    if res is not None:
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
                res = await cur.fetchone()
                await db.commit()
                return cur.get_cursor().rowcount
                # return res if not None else None


async def _get_all_subreddits():
    """returns - list = [] or  [{"name": xxx, "webhook": url}]"""
    _subreddits: list[dict[str, Union[str, None]]] = []
    async with asqlite.connect(DB_FILENAME) as db:
        async with db.cursor() as cur:
            await cur.execute("""SELECT name, id FROM subreddit""")
            res = await cur.fetchall()
            if res is None or len(res) == 0:
                return []
            for entry in res:
                res_webhook = await _get_webhook(arg=entry["id"])
                if res_webhook is not None:
                    res_webhook = res_webhook["url"]
                _subreddits.append({"name": entry["name"], "webhook": res_webhook})
        return _subreddits


async def _update_subreddit(name: str, webhook: Union[int, str]):
    # Check if the subreddit exists
    res = await _get_subreddit(name=name)
    if res is None:
        return None
    else:
        # Check if the webhook exists
        res = await _get_webhook(webhook)
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


# async def _get_webhook(id: Union[None, int] = None, url: Union[None, str] = None, name: Union[None, str] = None):
#     """returns `"name", "id", "url"` """
#     async with asqlite.connect(DB_FILENAME) as db:
#         async with db.cursor() as cur:
#             if id is None and url is None and name is None:
#                 return None
#             elif id is not None:
#                 await cur.execute("""SELECT name, id, url FROM webhook where id = ?""", id)
#             elif url is not None:
#                 await cur.execute("""SELECT name, id, url FROM webhook where url = ?""", url)
#             else:
#                 await cur.execute("""SELECT name, id url FROM webhook where name = ?""", name)
#             res = await cur.fetchone()
#             return res if not None else None

async def _get_webhook(arg: Union[str, int, None]):
    """returns `"name", "id", "url"` """
    async with asqlite.connect(DB_FILENAME) as db:
        async with db.cursor() as cur:
            if arg is None:
                return None
            elif type(arg) == int:
                await cur.execute("""SELECT name, id, url FROM webhook where id = ?""", arg)
            elif type(arg) == str and arg.startswith("http"):
                await cur.execute("""SELECT name, id, url FROM webhook where url = ?""", arg)
            else:
                await cur.execute("""SELECT name, id url FROM webhook where name = ?""", arg)
            res = await cur.fetchone()
            return res if not None else None


async def _add_webhook(name: str, url: str):
    res = await _get_webhook(arg=url)
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
    res = await _get_webhook(arg=id)
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
        self._file_dir = Path(__file__).parent

        # Used to possible keep track of edits to the DB to prevent un-needed DB lookups on submissions.
        self._recent_edit: bool = False

        # PRAW information
        self._reddit_secret = ""
        self._reddit_client_id = ""
        self._reddit_username = ""
        self._reddit_password = ""

        # Image hash and Url storage
        self._json: Path = self._file_dir.joinpath("reddit.json")
        self._url_list: set = set()  # []  # Recently sent URLs
        self._hash_list: set = set()  # []  # Recently hashed images
        self._url_prefixs: tuple[str, ...] = ("http://", "https://")

        # This is how many posts in each subreddit the script will look back.
        # By default the subreddit script looks at subreddits in `NEW` listing order.
        self._submission_limit = 30
        self._subreddits: list[dict[str, Union[str, None]]] = []  # {"subreddit" : "webhook_url"}

        # TODO - Set default last check as posix and write out blank values to json file. See line 221
        self._last_check: datetime = datetime.now(tz=timezone.utc)

        # This forces the timezone to change based upon your OS for better readability in your prints()
        # This script uses `UTC` for functionality purposes.
        self._system_tz = tzlocal.get_localzone()

        # Need to use the string representation of the time zone for `pytz` eg. `America/Los_Angeles`
        self._pytz = pytz.timezone(str(self._system_tz))

        # Purely used to fill out the user_agent parameter of PRAW
        self._sysos: str = sys.platform.title()
        # Used by URL lib for Hashes.
        self._User_Agent = UserAgent().chrome

        # Default value; change in `reddit_cog.ini`
        self._user_name: Union[str, None] = "Reddit Scrapper"

    async def cog_load(self):
        """Load DB and settings from reddit_cog.ini"""
        # Setup our DB tables
        async with asqlite.connect(DB_FILENAME) as db:
            await db.execute(SUBREDDIT_SETUP_SQL)
            await db.execute(WEBHOOK_SETUP_SQL)
        # Grab our PRAW settings
        await self._ini_load()
        # Grab our hash/url DB

        try:
            self._last_check = self.json_load()
        except:
            self._last_check: datetime = datetime.now(tz=timezone.utc)

        self._subreddits = await _get_all_subreddits()
        # TODO - Once finished un-comment code.
        # if self.check_loop.is_running() is False:
        #     self.check_loop.start()

    async def cog_unload(self):
        self.json_save()
        if self.check_loop.is_running():
            self.check_loop.stop()

    async def _ini_load(self):
        """Loads the Reddit login information and additional settings."""
        _setting_file: Path = self._file_dir.joinpath("reddit_cog.ini")
        if _setting_file.is_file():
            settings = ConfigParser(converters={"list": lambda setting: [value.strip() for value in setting.split(",")]})
            settings.read(_setting_file.as_posix())
            # PRAW SETTINGS
            self._reddit_secret: str = settings.get("PRAW", "reddit_secret")
            self._reddit_client_id: str = settings.get("PRAW", "reddit_client_id")
            self._reddit_username: str = settings.get("PRAW", "reddit_username")
            self._reddit_password: str = settings.get("PRAW", "reddit_password")

            # CONFIG
            _temp_name = settings.get("CONFIG", "username")
            if len(_temp_name):
                self._user_name = _temp_name

            self._reddit = praw.Reddit(
                client_id=self._reddit_client_id,
                client_secret=self._reddit_secret,
                user_agent=f"{self._sysos}: (by /u/{self._reddit_username})"
            )
        else:
            raise ValueError("Failed to load .ini")

    def json_load(self):
        last_check: datetime = datetime.now(tz=timezone.utc)

        if self._json.is_file() is False:
            with open(self._json, "x") as jfile:
                jfile.close()
                self.json_save()
                return last_check

        else:
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

    def json_save(self):
        # I generate an upper limit of the list based upon the subreddits times the submissin search limit;
        # this allows for configuration changes and not having to scale the limit of the list
        _temp_url_list: list[str] = []
        _temp_hash_list: list[str] = []

        limiter = (len(self._subreddits) * self._submission_limit) * 3

        # Turn our set into a list, truncate it via indexing then replace our current set.
        if len(self._url_list) > limiter:
            print(f'Trimming down url list...')
            _temp_url_list = list(self._url_list)
            _temp_url_list = _temp_url_list[len(self._url_list) - limiter:]
            self._url_list = set(_temp_url_list)

        if len(self._hash_list) > limiter:
            print(f'Trimming down hash list...')
            _temp_hash_list = list(self._hash_list)
            _temp_hash_list = _temp_hash_list[len(self._hash_list) - limiter:]
            self._hash_list = set(_temp_hash_list)

        data = {
            "last_check": self._last_check.timestamp(),
            "url_list": self._url_list,
            "hash_list": self._hash_list
        }
        with open(self._json, "w") as jfile:
            json.dump(data, jfile)
            print('Saving our settings...')
            jfile.close()

    @tasks.loop(minutes=1)
    async def check_loop(self):
        if self._recent_edit:
            self._subreddits = []
            self._subreddits = await _get_all_subreddits()
            self._recent_edit = False

        if self._subreddits == None:
            print("No Subreddits found...")
            return

        count = await self.subreddit_media_handler(last_check=self._last_check)
        self.json_save()
        if count >= 1:
            print(f'Finished Sending {str(count) + " Images" if count > 1 else str(count) + " Image"}')

    async def subreddit_media_handler(self, last_check: datetime):
        """Iterates through the subReddits Submissions and sends media_metadata"""
        count = 0
        found_post = False
        img_url: Union[str, None] = None
        img_url_to_send: list[str] = []
        # We check self._subreddits in `check_loop`
        assert self._subreddits

        for entry in self._subreddits:
            for sub, url in entry:
                sub = entry[sub]
                url = entry[url]
                if url == None:
                    continue

                cur_subreddit: Subreddit = self._reddit.subreddit(sub)
                # limit - controls how far back to go (true limit is 100 entries)
                # TODO - cur_subreddit.new() can fail if the subreddit is gone.
                for submission in cur_subreddit.new(limit=self._submission_limit):
                    post_time: datetime = datetime.fromtimestamp(submission.created_utc, tz=timezone.utc)
                    found_post = False
                    print(f'Checking subreddit {sub} -> submission title: {submission.title} submission post_time: {post_time.astimezone(self._pytz).ctime()} last_check: {last_check.astimezone(self._pytz).ctime()}')

                    if post_time >= last_check:  # The more recent time will be greater than..
                        # reset our img list
                        img_url_to_send = []
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
                                    img_url_to_send.append(img["s"]["u"])
                                    continue

                                else:
                                    continue

                        elif hasattr(submission, "url_overridden_by_dest"):
                            # print('Found url_overridden_by_dest')
                            # img_url = submission.url_overridden_by_dest
                            if submission.url_overridden_by_dest.startswith(self._url_prefixs):
                                img_url_to_send.append(submission.url_overridden_by_dest)
                            else:
                                continue

                        else:
                            continue

                        # if img_url != None:
                        if len(img_url_to_send) > 0:
                            for img_url in img_url_to_send:
                                if img_url in self._url_list:
                                    continue

                                self._url_list.add(img_url)
                                status: bool = await self.hash_process(img_url)

                                if status:
                                    found_post = True
                                    count += 1
                                    await self.webhook_send(url=url, content=f'**r/{sub}** ->  __[{submission.title}]({submission.url})__\n{img_url}\n')
                                    time.sleep(1)  # Soft buffer delay between sends to prevent rate limiting.

                if found_post == False:
                    print(f'No new Submissions in {sub} since {last_check.ctime()}')

        return count

    async def hash_process(self, img_url: str) -> bool:
        """Checks the Hash of the supplied url against our hash list."""
        # TODO - Make this ASYNC if at all possible.
        # async with aiohttp.ClientSession() as session:
        #     req = await session.request(method="", url=img_url, headers={'User-Agent': str(self._User_Agent)})
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
                self._hash_list.add(my_hash)
                return True

            else:
                print('Found a duplicate hash...')
                return False
        else:  # Failed to find a 'image'
            print(f'URL: {img_url} is not an image -> {req_open.headers.get_content_type()}')
            return False

    async def webhook_send(self, url: str, content: str):
        """Sends the Data to the Discord webhook"""
        data = {"content": content, "username": self._user_name}
        async with aiohttp.ClientSession() as session:
            result: ClientResponse = await session.post(url, json=data)
            if 200 <= result.status < 300:
                print(f"Webhook sent {result.status}")
            else:
                print(f"Not sent with {result.status}, response:\n{result.json()}")

    @commands.command(help="Add a subreddit to the DB", aliases=["rsadd", "rsa"])
    async def add_subreddit(self, context: commands.Context, sub: str):
        res: Row | None = await _add_subreddit(name=sub)
        if res is not None:
            return await context.send(content=f"Added `{res['name']}` to our database.")
        else:
            return await context.send(content=f"Unable to add `{sub}` to the database.")

    @commands.command(help="Remove a subreddit from the DB", aliases=["rsdel", "rsd"])
    async def del_subreddit(self, context: commands.Context, sub: str):
        res: int | None = await _del_subreddit(name=sub)
        await context.send(content=f"Removed {res} {'subreddit' if res else ''}")

    @commands.command(help="Update a subreddit with a Webhook", aliases=["rsupdate", "rsu"])
    async def update_subreddit(self, context: commands.Context, sub: str, webhook: Union[str, int]):
        res: Row | None = await _update_subreddit(name=sub, webhook=webhook)
        if res is None:
            return await context.send(content=f"Unable to update `{sub}`.")
        else:
            return await context.send(content=f"Updated `{sub}` in our database.")

    @commands.command(help="List of subreddits", aliases=["rslist", "rsl"])
    async def list_subreddit(self, context: commands.Context):
        res: list | None = await _get_all_subreddits()
        return await context.send(content="__Entries__:\n" + "\n".join([f"**{entry['name']}**" for entry in res]))

    @commands.command(help="Info about a subreddit", aliases=["rsinfo", "rsi"])
    async def info_subreddit(self, context: commands.Context, sub: str):
        res: Row | None = await _get_subreddit(name=sub)
        if res is not None:
            wh_res: Row | None = await _get_webhook(arg=res["webhook_id"])
        if wh_res is not None:
            return await context.send(content=f"**{sub}**\n> {wh_res['name']}\n> {wh_res['url']}")
        else:
            return await context.send(content=f"**{sub}**\n`No webhook assosciated with this subreddit`")

    # /redditsubreddit
    # ?rssadd
    # ?rssdel
    # ?rssupdate sub, wh
    # ?rsslist
    # ?rsinfo
    # /redditwebhook
    # ?rswh add url / rswha
    # ?rswh del url / rswhd
    # ?rsloop start/stop


async def setup(bot: commands.Bot):
    await bot.add_cog(Reddit_IS(bot))
