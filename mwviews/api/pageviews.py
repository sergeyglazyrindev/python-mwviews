import requests
import traceback
from requests.utils import quote
from datetime import date, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from .exceptions import ApiLimitExceeded

endpoints = {
    'article': 'https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article',
    'project': 'https://wikimedia.org/api/rest_v1/metrics/pageviews/aggregate',
    'top': 'https://wikimedia.org/api/rest_v1/metrics/pageviews/top',
}


def get_wikipedia_error(results, return_error=False):
    for result in results:
        if result.status_code == requests.codes.too_many:
            return ApiLimitExceeded()
    if return_error:
        return Exception(
            'The pageview API returned nothing useful at: {}'.format(
                [result.url for result in results]
            )
        )


def parse_date(stringDate):
    return datetime.strptime(stringDate.ljust(10, '0'), '%Y%m%d%H')


def format_date(d):
    return datetime.strftime(d, '%Y%m%d%H')


def timestamps_between(start, end, increment):
    # convert both start and end to datetime just in case either are dates
    start = datetime(start.year, start.month, start.day, getattr(start, 'hour', 0))
    end = datetime(end.year, end.month, end.day, getattr(end, 'hour', 0))

    while start <= end:
        yield start
        start += increment


def month_from_day(dt):
    return datetime(dt.year, dt.month, 1)


class PageviewsClient:

    def __init__(self, parallelism=10, custom_http_headers=None):
        """
        Create a PageviewsClient

        :Parameters:
            parallelism : The number of parallel threads to use when making
                          multiple requests to the API at the same time
        """
        self.parallelism = parallelism
        self.custom_http_headers = custom_http_headers

    def article_views(
            self, project, articles,
            access='all-access', agent='all-agents', granularity='daily',
            start=None, end=None):
        """
        Get pageview counts for one or more articles
        See `<https://wikimedia.org/api/rest_v1/metrics/pageviews/?doc\\
                #!/Pageviews_data/get_metrics_pageviews_per_article_project\\
                _access_agent_article_granularity_start_end>`_

        :Parameters:
            project : str
                a wikimedia project such as en.wikipedia or commons.wikimedia
            articles : list(str)
            access : str
                access method (desktop, mobile-web, mobile-app, or by default, all-access)
            agent : str
                user agent type (spider, user, bot, or by default, all-agents)
            end : str|date
                can be a datetime.date object or string in YYYYMMDD format
                default: today
            start : str|date
                can be a datetime.date object or string in YYYYMMDD format
                default: 30 days before end date
            granularity : str
                can be daily or monthly.  Daily is the only supported granularity in the
                back-end API, and monthly just pulls daily data and aggregates it

        :Returns:
            a nested dictionary that looks like: {
                start_date: {
                    article_1: view_count,
                    article_2: view_count,
                    ...
                    article_n: view_count,
                },
                ...
                end_date: {
                    article_1: view_count,
                    article_2: view_count,
                    ...
                    article_n: view_count,
                }
            }
            The view_count will be None where no data is available, to distinguish from 0

        TODO: probably doesn't handle unicode perfectly, look into it
        """
        endDate = end or date.today()
        if type(endDate) is not date:
            endDate = parse_date(end)

        startDate = start or endDate - timedelta(30)
        if type(startDate) is not date:
            startDate = parse_date(start)

        articles = [a.replace(' ', '_') for a in articles]
        articlesSafe = [quote(a, safe='') for a in articles]

        urls = [
            '/'.join([
                # granularity is faked on the client side, the API
                # currently only supports daily
                endpoints['article'], project, access, agent, a, 'daily',
                format_date(startDate), format_date(endDate),
            ])
            for a in articlesSafe
        ]

        outputDays = timestamps_between(startDate, endDate, timedelta(days=1))
        output = defaultdict(dict, {
            day : {a : None for a in articles} for day in outputDays
        })

        try:
            results = self.get_concurrent(urls)
            some_data_returned = False
            for result in results:
                result = result.json()
                if 'items' in result:
                    some_data_returned = True
                else:
                    continue
                for item in result['items']:
                    output[parse_date(item['timestamp'])][item['article']] = item['views']
            error = get_wikipedia_error(results, return_error=not some_data_returned)
            if error:
                raise error

            if granularity == 'monthly':
                output_monthly = {}
                for day, views_per_article in output.items():
                    month = month_from_day(day)
                    if month not in output_monthly:
                        output_monthly[month] = {a : None for a in articles}
                    for article, views in views_per_article.items():
                        if views:
                            updated_views = (output_monthly[month][article] or 0) + views
                            output_monthly[month][article] = updated_views

                output = output_monthly

            return output
        except:
            print('ERROR while fetching and parsing ' + str(urls))
            traceback.print_exc()
            raise

    def project_views(
            self, projects,
            access='all-access', agent='all-agents', granularity='daily',
            start=None, end=None):
        """
        Get pageview counts for one or more wikimedia projects
        See `<https://wikimedia.org/api/rest_v1/metrics/pageviews/?doc\\
                #!/Pageviews_data/get_metrics_pageviews_aggregate_project\\
                _access_agent_granularity_start_end>`_

        :Parameters:
            project : list(str)
                a list of wikimedia projects such as en.wikipedia or commons.wikimedia
            access : str
                access method (desktop, mobile-web, mobile-app, or by default, all-access)
            agent : str
                user agent type (spider, user, bot, or by default, all-agents)
            granularity : str
                the granularity of the timeseries to return (hourly, daily, or monthly)
            end : str|date
                can be a datetime.date object or string in YYYYMMDDHH format
                default: today
            start : str|date
                can be a datetime.date object or string in YYYYMMDDHH format
                default: 30 days before end date

        :Returns:
            a nested dictionary that looks like: {
                start_date: {
                    project_1: view_count,
                    project_2: view_count,
                    ...
                    project_n: view_count,
                },
                ...
                end_date: {
                    project_1: view_count,
                    project_2: view_count,
                    ...
                    project_n: view_count,
                }
            }
            The view_count will be None where no data is available, to distinguish from 0
        """
        endDate = end or date.today()
        if type(endDate) is not date:
            endDate = parse_date(end)

        startDate = start or endDate - timedelta(30)
        if type(startDate) is not date:
            startDate = parse_date(start)

        urls = [
            '/'.join([
                endpoints['project'], p, access, agent, granularity,
                format_date(startDate), format_date(endDate),
            ])
            for p in projects
        ]

        if granularity == 'hourly':
            increment = timedelta(hours=1)
        elif granularity == 'daily':
            increment = timedelta(days=1)
        elif granularity == 'monthly':
            increment = timedelta(months=1)

        outputDays = timestamps_between(startDate, endDate, increment)
        output = defaultdict(dict, {
            day : {p : None for p in projects} for day in outputDays
        })

        try:
            results = self.get_concurrent(urls)
            some_data_returned = False
            for result in results:
                result = result.json()
                if 'items' in result:
                    some_data_returned = True
                else:
                    continue
                for item in result['items']:
                    output[parse_date(item['timestamp'])][item['project']] = item['views']

            error = get_wikipedia_error(results, return_error=not some_data_returned)
            if error:
                raise error
            return output
        except:
            print('ERROR while fetching and parsing ' + str(urls))
            traceback.print_exc()
            raise

    def top_articles(
            self, project, access='all-access',
            year=None, month=None, day=None, limit=1000):
        """
        Get pageview counts for one or more articles
        See `<https://wikimedia.org/api/rest_v1/metrics/pageviews/?doc\\
                #!/Pageviews_data/get_metrics_pageviews_top_project\\
                _access_year_month_day>`_

        :Parameters:
            project : str
                a wikimedia project such as en.wikipedia or commons.wikimedia
            access : str
                access method (desktop, mobile-web, mobile-app, or by default, all-access)
            year : int
                default : yesterday's year
            month : int
                default : yesterday's month
            day : int
                default : yesterday's day
            limit : int
                limit the number of articles returned to only the top <limit>
                default : 1000

        :Returns:
            a sorted list of articles that looks like: [
                {
                    rank: <int>,
                    article: <str>,
                    views: <int>
                }
                ...
            ]
        """
        yesterday = date.today() - timedelta(days=1)
        year = str(year or yesterday.year)
        month = str(month or yesterday.month).rjust(2, '0')
        day = str(day or yesterday.day).rjust(2, '0')

        url = '/'.join([endpoints['top'], project, access, year, month, day])

        try:
            result = requests.get(url)
            json_res = result.json()
            if 'items' in json_res and len(json_res['items']) == 1:
                r = json_res['items'][0]['articles']
                r.sort(key=lambda x: x['rank'])
                return r[0:(limit)]
        except:
            print('ERROR while fetching or parsing ' + url)
            traceback.print_exc()
            raise

        raise get_wikipedia_error([result])

    def get_concurrent(self, urls):
        with ThreadPoolExecutor(self.parallelism) as executor:
            f = lambda url: requests.get(url, headers=self.custom_http_headers)
            return list(executor.map(f, urls))
