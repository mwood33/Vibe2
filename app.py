import os
import json
import math
import time
import requests
import anthropic
import yfinance as yf
from flask import Flask, jsonify, send_from_directory, request
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__, static_folder='static')
NEWS_API_KEY = os.environ.get('NEWS_API_KEY', '')

REDDIT_HEADERS = {'User-Agent': 'MarketDashboard/1.0 (personal research tool)'}

# Static name/currency maps so we avoid slow yfinance .info calls
SYMBOL_NAMES = {
    '^GSPC': 'S&P 500', '^DJI': 'Dow Jones', '^IXIC': 'NASDAQ',
    '^GSPTSE': 'TSX Composite', 'USDCAD=X': 'USD/CAD',
    'CL=F': 'WTI Crude', 'NG=F': 'Natural Gas', 'GC=F': 'Gold', 'HG=F': 'Copper',
    'CA10YT=RR': 'GoC 10-Year', '^TNX': 'US 10-Year',
    'SU.TO': 'Suncor', 'CNQ.TO': 'Canadian Natural', 'ENB.TO': 'Enbridge',
    'TD.TO': 'TD Bank', 'RY.TO': 'Royal Bank', 'BNS.TO': 'Scotiabank',
    'CM.TO': 'CIBC', 'BMO.TO': 'Bank of Montreal', 'TRP.TO': 'TC Energy',
    'IMO.TO': 'Imperial Oil', 'CVE.TO': 'Cenovus', 'FTS.TO': 'Fortis',
    'EMA.TO': 'Emera', 'PPL.TO': 'Pembina Pipeline', 'AQN.TO': 'Algonquin Power',
    'XOM': 'ExxonMobil', 'CVX': 'Chevron', 'JPM': 'JPMorgan', 'BAC': 'Bank of America',
    'GS': 'Goldman Sachs',
}

SYMBOL_CURRENCY = {s: 'CAD' for s in [
    'SU.TO', 'CNQ.TO', 'ENB.TO', 'TD.TO', 'RY.TO', 'BNS.TO',
    'CM.TO', 'BMO.TO', 'TRP.TO', 'IMO.TO', 'CVE.TO', 'FTS.TO',
    'EMA.TO', 'PPL.TO', 'AQN.TO',
]}

INDICES = ['^GSPC', '^DJI', '^IXIC', '^GSPTSE']
FOREX = ['USDCAD=X']
COMMODITIES = ['CL=F', 'NG=F', 'GC=F', 'HG=F']
RATES = ['CA10YT=RR', '^TNX']
MOVERS = [
    'SU.TO', 'CNQ.TO', 'ENB.TO', 'TD.TO', 'RY.TO', 'BNS.TO',
    'CM.TO', 'BMO.TO', 'TRP.TO', 'IMO.TO', 'CVE.TO', 'FTS.TO',
    'EMA.TO', 'PPL.TO', 'AQN.TO', 'XOM', 'CVX', 'JPM', 'BAC', 'GS',
]

POSITIVE_WORDS = {
    'surge', 'gain', 'rise', 'jump', 'boost', 'record', 'high', 'growth',
    'bull', 'profit', 'beat', 'strong', 'soar', 'rally', 'approve',
    'invest', 'expand', 'recover', 'upgrade', 'outperform', 'wins', 'win',
    'advances', 'advance', 'exceeds', 'breakthrough', 'positive',
}
NEGATIVE_WORDS = {
    'crash', 'fall', 'drop', 'decline', 'loss', 'cut', 'crisis', 'fail',
    'low', 'bear', 'miss', 'weak', 'plunge', 'risk', 'warn', 'layoff',
    'debt', 'default', 'downgrade', 'underperform', 'loses', 'lose',
    'slumps', 'slump', 'tumbles', 'tumble', 'fears', 'fear', 'negative',
}


def fetch_ticker(sym):
    try:
        hist = yf.Ticker(sym).history(period='5d', auto_adjust=True)
        if len(hist) < 2:
            return sym, {'error': 'Insufficient data'}
        price = float(hist['Close'].iloc[-1])
        prev = float(hist['Close'].iloc[-2])
        change = price - prev
        pct = (change / prev) * 100 if prev else 0
        return sym, {
            'price': round(price, 4),
            'change': round(change, 4),
            'pct_change': round(pct, 2),
            'name': SYMBOL_NAMES.get(sym, sym),
            'currency': SYMBOL_CURRENCY.get(sym, 'USD'),
        }
    except Exception as e:
        return sym, {'error': str(e)}


def yf_batch(symbols):
    with ThreadPoolExecutor(max_workers=15) as ex:
        futures = [ex.submit(fetch_ticker, s) for s in symbols]
        return dict(f.result() for f in as_completed(futures))


def get_boc_rate():
    try:
        # Try the policy rate group endpoint first
        r = requests.get(
            'https://www.bankofcanada.ca/valet/observations/group/policy_interest_rate/json?recent=1',
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        obs = data.get('observations', [{}])[-1]
        # Find the overnight rate series (not the bank rate)
        for key, val in obs.items():
            if key.startswith('V') and isinstance(val, dict):
                rate = val.get('v')
                if rate:
                    return {'rate': float(rate), 'date': obs.get('d', '')}
        return {'error': 'Series not found'}
    except Exception:
        pass

    try:
        # Fallback: individual series
        r = requests.get(
            'https://www.bankofcanada.ca/valet/observations/V39079/json?recent=1',
            timeout=10,
        )
        r.raise_for_status()
        obs = r.json()['observations'][-1]
        return {'rate': float(obs['V39079']['v']), 'date': obs['d']}
    except Exception as e:
        return {'error': str(e)}


def get_news(query, num=7):
    if not NEWS_API_KEY:
        return []
    try:
        r = requests.get(
            'https://newsapi.org/v2/everything',
            params={
                'q': query,
                'language': 'en',
                'sortBy': 'publishedAt',
                'pageSize': num,
                'apiKey': NEWS_API_KEY,
            },
            timeout=12,
        )
        r.raise_for_status()
        return [
            {
                'title': a['title'],
                'url': a['url'],
                'source': a['source']['name'],
                'published': a['publishedAt'],
                'description': a.get('description') or '',
            }
            for a in r.json().get('articles', [])
            if a.get('title') and '[Removed]' not in a.get('title', '')
        ]
    except Exception as e:
        return [{'error': str(e)}]


def simple_sentiment(text):
    words = set(text.lower().split())
    pos = len(words & POSITIVE_WORDS)
    neg = len(words & NEGATIVE_WORDS)
    return 'positive' if pos > neg else 'negative' if neg > pos else 'neutral'


def get_reddit_trending():
    subs = ['canada', 'alberta', 'PersonalFinanceCanada', 'CanadaPolitics', 'CanadianInvestor', 'oilsands']
    posts = []
    for sub in subs:
        try:
            r = requests.get(
                f'https://www.reddit.com/r/{sub}/hot.json?limit=20',
                headers=REDDIT_HEADERS,
                timeout=10,
            )
            if r.status_code != 200:
                continue
            for child in r.json().get('data', {}).get('children', []):
                p = child['data']
                if p.get('stickied') or not p.get('title'):
                    continue
                score = p.get('score', 0)
                ratio = p.get('upvote_ratio', 0.5)
                comments = p.get('num_comments', 0)
                engagement = score * ratio * (1 + math.log1p(comments))
                posts.append({
                    'title': p['title'],
                    'url': 'https://reddit.com' + p.get('permalink', ''),
                    'subreddit': sub,
                    'score': score,
                    'comments': comments,
                    'engagement': round(engagement, 1),
                    'sentiment': simple_sentiment(p['title']),
                    'created_utc': p.get('created_utc'),
                })
        except Exception:
            continue
    posts.sort(key=lambda x: x['engagement'], reverse=True)
    return posts[:15]


def fmt_quote(quotes, sym, label):
    q = quotes.get(sym, {})
    return {
        'symbol': sym,
        'label': label,
        'price': q.get('price'),
        'change': q.get('change'),
        'pct_change': q.get('pct_change'),
        'currency': q.get('currency', 'USD'),
        'name': q.get('name', sym),
    }


QUESTIONS_PATH = os.path.join(os.path.dirname(__file__), 'data', 'questions.json')

EXTRACTION_SCHEMA = {
    'type': 'object',
    'properties': {
        'questions': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'question':     {'type': 'string'},
                    'answer':       {'type': 'integer'},
                    'context':      {'type': 'string'},
                    'category':     {'type': 'string'},
                    'report_title': {'type': 'string'},
                    'date':         {'type': 'string'},
                },
                'required': ['question', 'answer', 'context', 'category', 'report_title', 'date'],
                'additionalProperties': False,
            },
        },
    },
    'required': ['questions'],
    'additionalProperties': False,
}

EXTRACTION_PROMPT = """You are analyzing a public opinion polling report from INNOVATIVE Research Group, a Canadian research firm.

Extract every polling finding that would make an interesting game question where players guess percentages.

Good findings to extract:
- A clear percentage of a named group (Canadians, Albertans, young Canadians aged 18-34, etc.)
- Opinions, attitudes, or stated behaviours
- Surprising or counter-intuitive results that would challenge players
- Results that are clearly framed in the slides (headline numbers, key callouts)

For EACH finding, output:
- question: Frame as "What percentage of [group] [said/supported/agreed/believed] [finding]?" — self-contained, no jargon
- answer: The percentage as a plain integer 0-100 (e.g. 63, not "63%")
- context: 1-2 sentences about survey methodology, sample size, and date if available
- category: One of: Economy, Housing, Healthcare, Environment, Federal Politics, Social Policy, Technology, Public Safety, Defence & Security, Regional Politics, Immigration, Indigenous Affairs, Media & Trust
- report_title: The full title of the report or survey as shown
- date: Survey date in YYYY-MM-DD format (use YYYY-MM-01 if only month/year known, YYYY-01-01 if only year)

Skip findings that: are ambiguous, lack a clear percentage, or are about horse-race election vote intention (party %s).
Output all valid findings you can find."""


def load_questions():
    try:
        with open(QUESTIONS_PATH, 'r') as f:
            return json.load(f)
    except Exception:
        return []


def save_questions(questions):
    with open(QUESTIONS_PATH, 'w') as f:
        json.dump(questions, f, indent=2)


@app.route('/')
def index():
    return send_from_directory('static', 'game.html')


@app.route('/admin')
def admin():
    return send_from_directory('static', 'admin.html')


@app.route('/dashboard')
def dashboard():
    return send_from_directory('static', 'index.html')


@app.route('/api/questions')
def get_questions():
    return jsonify(load_questions())


@app.route('/api/question/daily')
def get_daily_question():
    questions = load_questions()
    if not questions:
        return jsonify({'error': 'no questions'}), 404
    from datetime import date
    epoch = date(2025, 1, 1)
    today = date.today()
    idx = (today - epoch).days % len(questions)
    return jsonify(questions[idx])


@app.route('/api/admin/process-pdf', methods=['POST'])
def process_pdf():
    api_key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        return jsonify({'error': 'ANTHROPIC_API_KEY not set on server'}), 500

    if 'pdf' not in request.files:
        return jsonify({'error': 'No PDF file provided'}), 400

    pdf_file = request.files['pdf']
    pdf_bytes = pdf_file.read()
    if not pdf_bytes:
        return jsonify({'error': 'Empty file'}), 400

    client = anthropic.Anthropic(api_key=api_key)
    uploaded = None
    try:
        uploaded = client.beta.files.upload(
            file=(pdf_file.filename or 'report.pdf', pdf_bytes, 'application/pdf'),
        )

        response = client.beta.messages.create(
            model='claude-opus-4-7',
            max_tokens=16000,
            betas=['files-api-2025-04-14'],
            output_config={'format': {'type': 'json_schema', 'schema': EXTRACTION_SCHEMA}},
            messages=[{
                'role': 'user',
                'content': [
                    {
                        'type': 'document',
                        'source': {'type': 'file', 'file_id': uploaded.id},
                    },
                    {'type': 'text', 'text': EXTRACTION_PROMPT},
                ],
            }],
        )

        raw = next(b.text for b in response.content if b.type == 'text')
        data = json.loads(raw)
        questions = data.get('questions', [])

        stamp = int(time.time())
        for i, q in enumerate(questions):
            q['id'] = f'irg-{stamp}-{i}'
            q['source'] = 'INNOVATIVE Research Group'
            q['source_url'] = 'https://www.innovativeresearch.ca/insights/'

        return jsonify({'questions': questions})

    except Exception as e:
        return jsonify({'error': str(e)}), 500

    finally:
        if uploaded:
            try:
                client.beta.files.delete(uploaded.id)
            except Exception:
                pass


@app.route('/api/admin/add-questions', methods=['POST'])
def add_questions():
    data = request.get_json(force=True)
    new_qs = data.get('questions', [])
    if not new_qs:
        return jsonify({'error': 'No questions provided'}), 400

    existing = load_questions()
    existing_ids = {q['id'] for q in existing}
    added = 0
    for q in new_qs:
        if q.get('id') not in existing_ids:
            existing.append(q)
            added += 1

    save_questions(existing)
    return jsonify({'added': added, 'total': len(existing)})


@app.route('/api/all')
def get_all():
    all_symbols = INDICES + FOREX + COMMODITIES + RATES + MOVERS

    with ThreadPoolExecutor(max_workers=8) as ex:
        fq = ex.submit(yf_batch, all_symbols)
        fb = ex.submit(get_boc_rate)
        fn_oil = ex.submit(get_news, '"oil sands" OR "oilsands" OR "tar sands" Alberta energy')
        fn_util = ex.submit(get_news, 'Canada utilities electricity "Fortis" OR "Emera" OR "Hydro One" OR "Algonquin"')
        fn_bank = ex.submit(get_news, 'Canada bank "TD Bank" OR "RBC" OR "Scotiabank" OR "BMO" OR "CIBC" financial')
        fn_pol = ex.submit(get_news, 'Canada "prime minister" polling election "Carney" OR "Poilievre" OR "NDP"')
        fr = ex.submit(get_reddit_trending)

    quotes = fq.result()

    mover_data = []
    for sym in MOVERS:
        q = quotes.get(sym, {})
        if q.get('pct_change') is not None:
            mover_data.append({
                'symbol': sym,
                'name': q.get('name', sym),
                'pct_change': q.get('pct_change'),
                'price': q.get('price'),
                'currency': q.get('currency', 'USD'),
            })
    mover_data.sort(key=lambda x: x['pct_change'], reverse=True)

    return jsonify({
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'indices': [
            fmt_quote(quotes, '^GSPC', 'S&P 500'),
            fmt_quote(quotes, '^DJI', 'Dow Jones'),
            fmt_quote(quotes, '^IXIC', 'NASDAQ'),
            fmt_quote(quotes, '^GSPTSE', 'TSX Composite'),
        ],
        'forex': fmt_quote(quotes, 'USDCAD=X', 'USD / CAD'),
        'commodities': [
            fmt_quote(quotes, 'CL=F', 'WTI Crude'),
            fmt_quote(quotes, 'NG=F', 'Natural Gas'),
            fmt_quote(quotes, 'GC=F', 'Gold'),
            fmt_quote(quotes, 'HG=F', 'Copper'),
        ],
        'rates': {
            'boc': fb.result(),
            'ca_10yr': fmt_quote(quotes, 'CA10YT=RR', 'GoC 10-Year'),
            'us_10yr': fmt_quote(quotes, '^TNX', 'US 10-Year'),
        },
        'movers': {
            'top': mover_data[:5],
            'bottom': list(reversed(mover_data[-5:])),
        },
        'news': {
            'oil_sands': fn_oil.result(),
            'utilities': fn_util.result(),
            'banking': fn_bank.result(),
            'politics': fn_pol.result(),
        },
        'reddit': fr.result(),
        'news_enabled': bool(NEWS_API_KEY),
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
