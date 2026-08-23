"""Apify Social Scraper modules."""
from .linkedin_scraper import LinkedInScraper
from .reddit_scraper import RedditScraper
from .twitter_scraper import TwitterScraper
from .blog_scraper import BlogScraper
from .sec_scraper import SECScraper
from .news_scraper import GoogleNewsScraper
from .patents_scraper import PatentsScraper

__all__ = [
    "LinkedInScraper",
    "RedditScraper",
    "TwitterScraper",
    "BlogScraper",
    "SECScraper",
    "GoogleNewsScraper",
    "PatentsScraper",
]
