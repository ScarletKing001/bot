import asyncio
import logging
import random
import textwrap
from datetime import datetime, timedelta
from typing import List

from aiohttp import BasicAuth
from discord import Colour, Embed, Message, TextChannel
from discord.ext import tasks
from discord.ext.commands import Bot, Cog, Context, group

from bot.constants import Channels, ERROR_REPLIES, Reddit as RedditConfig, STAFF_ROLES
from bot.converters import Subreddit
from bot.decorators import with_role
from bot.pagination import LinePaginator

log = logging.getLogger(__name__)


class Reddit(Cog):
    """Track subreddit posts and show detailed statistics about them."""

    # Change your client's User-Agent string to something unique and descriptive,
    # including the target platform, a unique application identifier, a version string,
    # and your username as contact information, in the following format:
    # <platform>:<app ID>:<version string> (by /u/<reddit username>)
    USER_AGENT = "docker-python3:Discord Bot of PythonDiscord (https://pythondiscord.com/):v?.?.? (by /u/PythonDiscord)"
    URL = "https://www.reddit.com"
    OAUTH_URL = "https://oauth.reddit.com"
    MAX_FETCH_RETRIES = 3

    def __init__(self, bot: Bot):
        self.bot = bot

        self.reddit_channel = None

        self.prev_lengths = {}
        self.last_ids = {}

        self.new_posts_task = None
        self.top_weekly_posts_task = None

        self.bot.loop.create_task(self.init_reddit_polling())

    @tasks.loop(hours=0.99)  # access tokens are valid for one hour
    async def refresh_access_token(self) -> None:
        """Refresh Reddits access token."""
        headers = {"Authorization": self.client_auth}
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token
        }

        response = await self.bot.http_session.post(
            url=f"{self.URL}/api/v1/access_token",
            headers=headers,
            data=data,
        )

        content = await response.json()
        self.access_token = content["access_token"]
        self.headers = {
            "Authorization": "bearer " + self.access_token,
            "User-Agent": self.USER_AGENT
        }

    @refresh_access_token.before_loop
    async def get_tokens(self) -> None:
        """Get Reddit access and refresh tokens."""
        headers = {"User-Agent": self.USER_AGENT}
        data = {
            "grant_type": "client_credentials",
            "duration": "permanent"
        }

        self.client_auth = BasicAuth(RedditConfig.client_id, RedditConfig.secret)

        response = await self.bot.http_session.post(
            url=f"{self.URL}/api/v1/access_token",
            headers=headers,
            auth=self.client_auth,
            data=data
        )

        if response.status == 200 and response.content_type == "application/json":
            content = await response.json()
            self.access_token = content["access_token"]
            self.refresh_token = content["refresh_token"]
            self.headers = {
                "Authorization": "bearer " + self.access_token,
                "User-Agent": self.USER_AGENT
            }
        else:
            log.error("Authentication with Reddit API failed. Unloading extension.")
            self.bot.remove_cog(self.__class__.__name__)
            return

    async def fetch_posts(self, route: str, *, amount: int = 25, params: dict = None) -> List[dict]:
        """A helper method to fetch a certain amount of Reddit posts at a given route."""
        # Reddit's JSON responses only provide 25 posts at most.
        if not 25 >= amount > 0:
            raise ValueError("Invalid amount of subreddit posts requested.")

        if params is None:
            params = {}

        url = f"{self.OAUTH_URL}/{route}"
        for _ in range(self.MAX_FETCH_RETRIES):
            response = await self.bot.http_session.get(
                url=url,
                headers=self.headers,
                params=params
            )
            if response.status == 200 and response.content_type == 'application/json':
                # Got appropriate response - process and return.
                content = await response.json()
                posts = content["data"]["children"]
                return posts[:amount]

            await asyncio.sleep(3)

        log.debug(f"Invalid response from: {url} - status code {response.status}, mimetype {response.content_type}")
        return list()  # Failed to get appropriate response within allowed number of retries.

    async def send_top_posts(
        self, channel: TextChannel, subreddit: Subreddit, content: str = None, time: str = "all"
    ) -> Message:
        """Create an embed for the top posts, then send it in a given TextChannel."""
        # Create the new spicy embed.
        embed = Embed()
        embed.description = ""

        # Get the posts
        async with channel.typing():
            posts = await self.fetch_posts(
                route=f"{subreddit}/top",
                amount=5,
                params={
                    "t": time
                }
            )

        if not posts:
            embed.title = random.choice(ERROR_REPLIES)
            embed.colour = Colour.red()
            embed.description = (
                "Sorry! We couldn't find any posts from that subreddit. "
                "If this problem persists, please let us know."
            )

            return await channel.send(
                embed=embed
            )

        for post in posts:
            data = post["data"]

            text = data["selftext"]
            if text:
                text = textwrap.shorten(text, width=128, placeholder="...")
                text += "\n"  # Add newline to separate embed info

            ups = data["ups"]
            comments = data["num_comments"]
            author = data["author"]

            title = textwrap.shorten(data["title"], width=64, placeholder="...")
            link = self.URL + data["permalink"]

            embed.description += (
                f"[**{title}**]({link})\n"
                f"{text}"
                f"| {ups} upvotes | {comments} comments | u/{author} | {subreddit} |\n\n"
            )

        embed.colour = Colour.blurple()

        return await channel.send(
            content=content,
            embed=embed
        )

    async def poll_new_posts(self) -> None:
        """Periodically search for new subreddit posts."""
        while True:
            await asyncio.sleep(RedditConfig.request_delay)

            for subreddit in RedditConfig.subreddits:
                # Make a HEAD request to the subreddit
                head_response = await self.bot.http_session.head(
                    url=f"{self.OAUTH_URL}/{subreddit}/new.rss",
                    headers=self.headers
                )

                content_length = head_response.headers["content-length"]

                # If the content is the same size as before, assume there's no new posts.
                if content_length == self.prev_lengths.get(subreddit, None):
                    continue

                self.prev_lengths[subreddit] = content_length

                # Now we can actually fetch the new data
                posts = await self.fetch_posts(f"{subreddit}/new")
                new_posts = []

                # Only show new posts if we've checked before.
                if subreddit in self.last_ids:
                    for post in posts:
                        data = post["data"]

                        # Convert the ID to an integer for easy comparison.
                        int_id = int(data["id"], 36)

                        # If we've already seen this post, finish checking
                        if int_id <= self.last_ids[subreddit]:
                            break

                        embed_data = {
                            "title": textwrap.shorten(data["title"], width=64, placeholder="..."),
                            "text": textwrap.shorten(data["selftext"], width=128, placeholder="..."),
                            "url": self.URL + data["permalink"],
                            "author": data["author"]
                        }

                        new_posts.append(embed_data)

                self.last_ids[subreddit] = int(posts[0]["data"]["id"], 36)

                # Send all of the new posts as spicy embeds
                for data in new_posts:
                    embed = Embed()

                    embed.title = data["title"]
                    embed.url = data["url"]
                    embed.description = data["text"]
                    embed.set_footer(text=f"Posted by u/{data['author']} in {subreddit}")
                    embed.colour = Colour.blurple()

                    await self.reddit_channel.send(embed=embed)

                log.trace(f"Sent {len(new_posts)} new {subreddit} posts to channel {self.reddit_channel.id}.")

    async def poll_top_weekly_posts(self) -> None:
        """Post a summary of the top posts every week."""
        while True:
            now = datetime.utcnow()

            # Calculate the amount of seconds until midnight next monday.
            monday = now + timedelta(days=7 - now.weekday())
            monday = monday.replace(hour=0, minute=0, second=0)
            until_monday = (monday - now).total_seconds()

            await asyncio.sleep(until_monday)

            for subreddit in RedditConfig.subreddits:
                # Send and pin the new weekly posts.
                message = await self.send_top_posts(
                    channel=self.reddit_channel,
                    subreddit=subreddit,
                    content=f"This week's top {subreddit} posts have arrived!",
                    time="week"
                )

                if subreddit.lower() == "r/python":
                    # Remove the oldest pins so that only 5 remain at most.
                    pins = await self.reddit_channel.pins()

                    while len(pins) >= 5:
                        await pins[-1].unpin()
                        del pins[-1]

                    await message.pin()

    @group(name="reddit", invoke_without_command=True)
    async def reddit_group(self, ctx: Context) -> None:
        """View the top posts from various subreddits."""
        await ctx.invoke(self.bot.get_command("help"), "reddit")

    @reddit_group.command(name="top")
    async def top_command(self, ctx: Context, subreddit: Subreddit = "r/Python") -> None:
        """Send the top posts of all time from a given subreddit."""
        await self.send_top_posts(
            channel=ctx.channel,
            subreddit=subreddit,
            content=f"Here are the top {subreddit} posts of all time!",
            time="all"
        )

    @reddit_group.command(name="daily")
    async def daily_command(self, ctx: Context, subreddit: Subreddit = "r/Python") -> None:
        """Send the top posts of today from a given subreddit."""
        await self.send_top_posts(
            channel=ctx.channel,
            subreddit=subreddit,
            content=f"Here are today's top {subreddit} posts!",
            time="day"
        )

    @reddit_group.command(name="weekly")
    async def weekly_command(self, ctx: Context, subreddit: Subreddit = "r/Python") -> None:
        """Send the top posts of this week from a given subreddit."""
        await self.send_top_posts(
            channel=ctx.channel,
            subreddit=subreddit,
            content=f"Here are this week's top {subreddit} posts!",
            time="week"
        )

    @with_role(*STAFF_ROLES)
    @reddit_group.command(name="subreddits", aliases=("subs",))
    async def subreddits_command(self, ctx: Context) -> None:
        """Send a paginated embed of all the subreddits we're relaying."""
        embed = Embed()
        embed.title = "Relayed subreddits."
        embed.colour = Colour.blurple()

        await LinePaginator.paginate(
            RedditConfig.subreddits,
            ctx, embed,
            footer_text="Use the reddit commands along with these to view their posts.",
            empty=False,
            max_lines=15
        )

    async def init_reddit_polling(self) -> None:
        """Initiate reddit post event loop."""
        await self.bot.wait_until_ready()
        self.reddit_channel = await self.bot.fetch_channel(Channels.reddit)
        self.refresh_access_token.start()

        if self.reddit_channel is not None:
            if self.new_posts_task is None:
                self.new_posts_task = self.bot.loop.create_task(self.poll_new_posts())
            if self.top_weekly_posts_task is None:
                self.top_weekly_posts_task = self.bot.loop.create_task(self.poll_top_weekly_posts())
        else:
            log.warning("Couldn't locate a channel for subreddit relaying.")


def setup(bot: Bot) -> None:
    """Reddit cog load."""
    bot.add_cog(Reddit(bot))
    log.info("Cog loaded: Reddit")
