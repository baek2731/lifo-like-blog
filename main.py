import os
import json
import re
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup
from github import Github, GithubException, Auth
from google import genai
from google.genai import types
from dotenv import load_dotenv

# =====================================================================
# ⚙️ [환경변수 로드 및 글로벌 설정]
# =====================================================================
# GitHub Actions 클라우드 환경: 환경변수는 GitHub Secrets에서 주입
# 로컬 테스트: .env 파일 사용 (load_dotenv가 자동으로 처리)
load_dotenv()

AUTO_MODE = True  # 클라우드 자동 실행 모드

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO_NAME = os.getenv("GITHUB_REPO_NAME")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY")

TARGET_SUBREDDITS = [
    "technology",
    "software",
    "gadgets",
    "gaming",
    "games",
]

# 클라우드 환경에서는 GitHub 저장소의 history.json을 직접 읽고 씁니다
HISTORY_FILE = "history.json"
OUTPUT_HTML = "sample_post.html"

client = genai.Client(api_key=GEMINI_API_KEY)
FLASH_MODEL = 'gemini-2.5-flash'

# [FIX] RSS 제목을 그대로 쓰면 내용과 미스매치 발생 — 제목은 항상 내용 기반으로 재생성
# [FIX] 글 잘림 방지 — generate_seo_post()에 max_output_tokens=8192 추가
# [FIX] "Where to preorder" 같은 쇼핑성 RSS 제목 필터링 추가


def load_history():
    """GitHub 저장소에서 history.json을 로드합니다."""
    try:
        auth = Auth.Token(GITHUB_TOKEN)
        g = Github(auth=auth)
        repo = g.get_repo(GITHUB_REPO_NAME)
        file = repo.get_contents("history.json")
        data = json.loads(file.decoded_content.decode('utf-8'))
        # 구버전 리스트 형식 자동 마이그레이션
        if isinstance(data, list):
            data = {"published_ids": data, "last_category": None, "last_published_date": None}
        # 필수 키 보장 (없으면 기본값 채움) — 구버전 dict 마이그레이션
        data.setdefault("published_ids", [])
        data.setdefault("published_titles", [])   # 뉴스RSS 토픽 중복방지용
        data.setdefault("last_category", None)
        data.setdefault("last_published_date", None)
        return data
    except Exception:
        return {"published_ids": [], "published_titles": [], "last_category": None, "last_published_date": None}


def save_history(history_data):
    """history.json을 GitHub 저장소에 저장합니다."""
    try:
        auth = Auth.Token(GITHUB_TOKEN)
        g = Github(auth=auth)
        repo = g.get_repo(GITHUB_REPO_NAME)
        content = json.dumps(history_data, ensure_ascii=False, indent=4)
        try:
            file = repo.get_contents("history.json")
            repo.update_file(
                path="history.json",
                message="chore: update history",
                content=content,
                sha=file.sha
            )
        except GithubException:
            repo.create_file(
                path="history.json",
                message="chore: create history",
                content=content
            )
        print("✅ history.json GitHub 저장 완료")
    except Exception as e:
        print(f"⚠️ history.json 저장 실패: {e}")


def log_to_github(message, log_type="INFO"):
    """GitHub 저장소 logs/ 폴더에 날짜별 로그를 저장합니다."""
    try:
        now_utc = datetime.now(timezone.utc)
        log_date = now_utc.strftime("%Y-%m-%d")
        log_path = f"logs/{log_date}.txt"
        timestamp = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        log_line = f"[{timestamp}] [{log_type}] {message}\n"

        auth = Auth.Token(GITHUB_TOKEN)
        g = Github(auth=auth)
        repo = g.get_repo(GITHUB_REPO_NAME)

        try:
            file = repo.get_contents(log_path)
            existing = file.decoded_content.decode('utf-8')
            repo.update_file(
                path=log_path,
                message=f"log: {log_date}",
                content=existing + log_line,
                sha=file.sha
            )
        except GithubException:
            repo.create_file(
                path=log_path,
                message=f"log: {log_date}",
                content=log_line
            )
    except Exception as e:
        print(f"⚠️ 로그 저장 실패: {e}")


def extract_image_keyword(title):
    """포스트 제목에서 Unsplash 검색용 핵심 키워드를 추출합니다."""
    clean = re.sub(r'[^\w\s]', ' ', title)
    words = clean.split()
    stopwords = {
        'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been',
        'has', 'have', 'had', 'do', 'does', 'did', 'will', 'would',
        'could', 'should', 'may', 'might', 'to', 'of', 'in', 'for',
        'on', 'with', 'at', 'by', 'from', 'up', 'about', 'into',
        'through', 'and', 'or', 'but', 'if', 'as', 'it', 'its',
        'this', 'that', 'they', 'their', 'what', 'which', 'who',
        'how', 'when', 'where', 'why', 'all', 'not', 'no', 'so',
        'megathread', 'weekly', 'monthly', 'daily', 'thread',
        'discussion', 'official', 'update', 'news', 'new', 'old',
        'help', 'question', 'ask', 'announcement', 'psa', 'rant',
        'review', 'spoiler', 'meta', 'mod', 'pinned', 'sticky'
    }
    keywords = [w for w in words if w.lower() not in stopwords and len(w) > 2]
    return ' '.join(keywords[:3])


def fetch_unsplash_image(keyword):
    """Unsplash API로 키워드 관련 이미지를 공식 라이선스로 가져옵니다. 3단계 재시도 포함."""
    print(f"🖼️ Unsplash에서 '{keyword}' 관련 이미지 검색 중...")

    def _search(query):
        try:
            params = {
                "query": query,
                "per_page": 1,
                "orientation": "landscape",
                "content_filter": "high"
            }
            headers = {"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"}
            response = requests.get(
                "https://api.unsplash.com/search/photos",
                params=params, headers=headers, timeout=10
            )
            if response.status_code != 200:
                return None
            data = response.json()
            if not data.get("results"):
                return None
            return data["results"][0]
        except Exception:
            return None

    try:
        # 1차 시도 — 전체 키워드
        photo = _search(keyword)

        # 2차 시도 — 첫 번째 키워드만
        if not photo:
            first_keyword = keyword.split()[0] if keyword.split() else keyword
            print(f"  • 재시도: '{first_keyword}' 단일 키워드로 검색 중...")
            photo = _search(first_keyword)

        # 3차 시도 — 일반 기술/게임 키워드
        if not photo:
            print(f"  • 재시도: 일반 키워드로 검색 중...")
            photo = _search("technology digital")

        if not photo:
            print(f"⚠️ Unsplash 이미지를 찾을 수 없습니다. 폴백 이미지를 사용합니다.")
            return None

        alt = photo.get("alt_description", "")
        if not alt or len(alt.strip()) < 5:
            alt = keyword

        # urls.regular에서 직접 가져오고 파라미터만 간소화
        raw_url = photo["urls"]["regular"]
        clean_url = re.sub(r'\?.*', '?w=1080&q=80', raw_url)

        return {
            "url": clean_url,
            "alt": alt,
            "photographer_name": photo["user"]["name"],
            "photographer_url": photo["user"]["links"]["html"] + "?utm_source=lifolike&utm_medium=referral"
        }

    except Exception as e:
        print(f"⚠️ Unsplash 이미지 로드 실패: {e}")
        return None


def clean_reddit_title(title):
    """Reddit 제목에서 SEO에 불필요한 접두어/태그를 제거합니다."""
    # 대괄호로 시작하는 태그 제거 (예: [MEGATHREAD], [WEEKLY], [PSA] 등)
    title = re.sub(r'^\[.*?\]\s*', '', title)
    # 소괄호로 시작하는 태그 제거
    title = re.sub(r'^\(.*?\)\s*', '', title)
    # 앞뒤 공백 제거
    title = title.strip()
    # 첫 글자 대문자화
    if title:
        title = title[0].upper() + title[1:]
    return title


# [FIX] 쇼핑/가이드성 RSS 제목 필터링 — 내용과 미스매치 방지
TITLE_BLOCKLIST_PREFIXES = [
    "where to preorder",
    "where to buy",
    "best deals",
    "how to get",
    "review:",
    "hands on:",
    "hands-on:",
    "giveaway:",
]

def is_blocked_title(title):
    """쇼핑/가이드성 제목 필터링."""
    t = title.lower().strip()
    return any(t.startswith(prefix) for prefix in TITLE_BLOCKLIST_PREFIXES)


# [FIX] 토픽 유사도 중복 방지용 불용어 (흔한 단어 + 숫자류는 카운트 제외)
TOPIC_STOPWORDS = {
    'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'to', 'of',
    'in', 'for', 'on', 'with', 'at', 'by', 'from', 'and', 'or', 'but', 'as',
    'it', 'its', 'this', 'that', 'new', 'now', 'will', 'has', 'have', 'game',
    'games', 'gaming', 'tech', 'update', 'news', 'launch', 'release', 'first',
    'more', 'get', 'gets', 'how', 'why', 'what', 'when', 'you', 'your',
}

def _topic_keywords(title):
    """제목에서 중복 판정용 핵심 키워드 집합을 추출합니다. 숫자/불용어/2글자 이하 제외."""
    words = re.sub(r'[^\w\s]', ' ', title.lower()).split()
    keywords = set()
    for w in words:
        if w in TOPIC_STOPWORDS:
            continue
        if len(w) <= 2:          # 2글자 이하 제외 (vi, 2, ai 등 애매한 것 제거)
            continue
        if w.isdigit():          # 순수 숫자 제외 (2028, 6 등)
            continue
        keywords.add(w)
    return keywords

def is_duplicate_topic(title, published_titles, threshold=3):
    """이미 발행된 제목들과 핵심 키워드가 threshold개 이상 겹치면 중복으로 판정합니다."""
    current = _topic_keywords(title)
    if len(current) < threshold:  # 키워드가 너무 적으면 판정 보류 (오탐 방지)
        return False
    for past in published_titles:
        past_kw = _topic_keywords(past)
        overlap = current & past_kw
        if len(overlap) >= threshold:
            return True
    return False


NEWS_RSS_SOURCES = [
    {"url": "https://feeds.arstechnica.com/arstechnica/index", "category": "tech"},
    {"url": "https://www.theverge.com/rss/index.xml", "category": "tech"},
    {"url": "https://techcrunch.com/feed/", "category": "tech"},
    {"url": "https://feeds.feedburner.com/ign/all", "category": "gaming"},
    {"url": "https://www.eurogamer.net/feed", "category": "gaming"},
    {"url": "https://kotaku.com/rss", "category": "gaming"},
]


def fetch_news_rss(history):
    """공식 뉴스 RSS에서 최신 기사를 수집합니다."""
    print("📰 뉴스 RSS 스캔 중 (Ars Technica, The Verge, IGN 등)...")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    news_items = []
    now_utc = datetime.now(timezone.utc)

    for source in NEWS_RSS_SOURCES:
        try:
            response = requests.get(source["url"], headers=headers, timeout=10)
            if response.status_code != 200:
                continue

            soup = BeautifulSoup(response.content, "xml")
            items = soup.find_all("item") or soup.find_all("entry")

            for item in items[:3]:
                title_tag = item.find("title")
                title = title_tag.text.strip() if title_tag else ""
                if not title:
                    continue

                # [FIX] 쇼핑/가이드성 제목 필터링
                if is_blocked_title(title):
                    print(f"  • [SKIP] 쇼핑/가이드 제목 필터링: {title[:50]}")
                    continue

                link_tag = item.find("link")
                link = ""
                if link_tag:
                    link = link_tag.get("href") or link_tag.text.strip()

                pub_tag = item.find("pubDate") or item.find("published") or item.find("updated")
                if pub_tag:
                    try:
                        pub_text = pub_tag.text.strip()
                        from email.utils import parsedate_to_datetime
                        try:
                            pub_time = parsedate_to_datetime(pub_text)
                        except Exception:
                            pub_time = datetime.fromisoformat(pub_text.replace("Z", "+00:00"))
                        hours_old = (now_utc - pub_time).total_seconds() / 3600
                        if hours_old > 48:
                            continue
                    except Exception:
                        pass

                desc_tag = item.find("description") or item.find("summary") or item.find("content")
                desc = ""
                if desc_tag:
                    desc = re.sub(r'<[^>]*>', '', desc_tag.text).strip()

                news_items.append({
                    "title": title,
                    "link": link,
                    "desc": desc[:500],
                    "source_url": source["url"],
                    "category": source["category"]
                })

        except Exception as e:
            print(f"⚠️ 뉴스 RSS 로드 실패 ({source['url'][:40]}): {e}")
            continue

    print(f"  • 뉴스 기사 {len(news_items)}개 수집 완료")
    return news_items


def fetch_reddit_context(title, subreddits):
    """뉴스 제목과 관련된 Reddit 커뮤니티 반응을 검색합니다."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    keywords = ' '.join(title.split()[:4])
    context_parts = []

    for sub in subreddits[:3]:
        try:
            search_url = f"https://www.reddit.com/r/{sub}/search.json?q={requests.utils.quote(keywords)}&sort=new&limit=3&restrict_sr=1"
            response = requests.get(search_url, headers=headers, timeout=8)
            if response.status_code != 200:
                continue
            data = response.json()
            posts = data.get("data", {}).get("children", [])
            for post in posts[:2]:
                p = post.get("data", {})
                post_title = p.get("title", "")
                selftext = p.get("selftext", "")[:300]
                if post_title:
                    context_parts.append(f"[r/{sub}] {post_title}: {selftext}")
        except Exception:
            continue

    return "\n".join(context_parts[:4]) if context_parts else ""


def fetch_global_trends(subreddits, history):
    """뉴스 RSS + Reddit 2단계 구조로 트렌드를 수집합니다."""
    print("🛰️ 글로벌 트렌드 스캔 시작 (뉴스 RSS + Reddit 2단계)...")
    now_utc = datetime.now(timezone.utc)

    # 1단계: 뉴스 RSS 수집
    news_items = fetch_news_rss(history)

    # 2단계: Reddit에서 단독 트렌드도 병행 수집
    print("🔍 Reddit 커뮤니티 보조 스캔 중...")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    reddit_only = []
    for sub in subreddits:
        url = f"https://www.reddit.com/r/{sub}/.rss"
        try:
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code != 200:
                continue
            soup = BeautifulSoup(response.content, "xml")
            entries = soup.find_all("entry")
            for entry in entries[:5]:
                link = entry.find("link")["href"] if entry.find("link") else ""
                id_match = re.search(r'/comments/([a-zA-Z0-9]+)/', link)
                if not id_match:
                    continue
                post_id = id_match.group(1)
                if post_id in history["published_ids"]:
                    continue
                updated_tag = entry.find("updated")
                if updated_tag:
                    try:
                        post_time = datetime.fromisoformat(updated_tag.text.replace("Z", "+00:00"))
                        hours_old = (now_utc - post_time).total_seconds() / 3600
                        if hours_old > 48:
                            continue
                    except Exception:
                        pass
                title = entry.find("title").text if entry.find("title") else "No Title"
                title = clean_reddit_title(title)
                content = entry.find("content").text if entry.find("content") else ""
                clean_content = re.sub(r'<[^>]*>', '', content).strip()
                reddit_only.append({
                    "id": post_id,
                    "subreddit": sub,
                    "title": title,
                    "link": link,
                    "content": clean_content[:2000],
                    "source": "reddit"
                })
        except Exception as e:
            print(f"⚠️ r/{sub} 로드 실패: {e}")
            continue

    # 뉴스 RSS 기사를 candidate 형식으로 변환 + Reddit 컨텍스트 결합
    candidates = []
    used_titles = set()
    published_ids = history.get("published_ids", [])
    published_titles = history.get("published_titles", [])

    for news in news_items:
        title_key = news["title"][:40].lower()
        if title_key in used_titles:   # 같은 실행 내 동일 기사 중복
            continue
        used_titles.add(title_key)

        # [FIX] URL 기반 고유 ID — 해시 충돌 제거
        news_id = f"news_{news['link']}"

        # [FIX] 이전에 발행한 뉴스 URL이면 스킵
        if news_id in published_ids:
            print(f"  • [DUP] 이미 발행된 뉴스 URL: {news['title'][:40]}")
            continue

        # [FIX] 같은 토픽(키워드 3개 이상 겹침)이 이미 발행됐으면 스킵
        if is_duplicate_topic(news["title"], published_titles):
            print(f"  • [DUP] 유사 토픽 이미 발행됨: {news['title'][:40]}")
            continue

        reddit_ctx = fetch_reddit_context(news["title"], subreddits)
        combined_content = f"[NEWS SOURCE: {news['link']}]\n{news['desc']}"
        if reddit_ctx:
            combined_content += f"\n\n[COMMUNITY REACTION]\n{reddit_ctx}"

        candidates.append({
            "id": news_id,
            "subreddit": news["category"],
            "title": news["title"],
            "link": news["link"],
            "content": combined_content[:2000],
            "desc": news["desc"],
            "source": "news",
            "news_url": news["link"]
        })

    # Reddit 단독 트렌드 추가 (뉴스에 없는 것) — 토픽 중복 체크 통과분만
    reddit_added = 0
    for r in reddit_only[:5]:
        if is_duplicate_topic(r["title"], published_titles):
            print(f"  • [DUP] 유사 토픽 이미 발행됨(Reddit): {r['title'][:40]}")
            continue
        candidates.append(r)
        reddit_added += 1

    print(f"✨ 총 {len(candidates)}개 후보 확보 (뉴스 {len(candidates) - reddit_added}개 + Reddit {reddit_added}개)")
    return candidates


def evaluate_filter_and_summarize_oneshot(candidates):
    """단 1번의 API 호출로 채점 + 카테고리 분류 + 한글 요약을 원샷으로 완수합니다."""
    print("🧠 지능형 통합 원샷 레이어 가동: 초고속 가치 평가 및 한국어 요약 동시 조립 중...")

    input_package = []
    for idx, cand in enumerate(candidates):
        input_package.append({
            "index": idx,
            "subreddit": cand['subreddit'],
            "title": cand['title'],
            "content_snippet": cand['content'][:250]
        })

    prompt = (
        "You are a master Google SEO strategist and elite tech/gaming trend analyst.\n"
        "Analyze the following list of Reddit threads and perform FOUR tasks for each item:\n"
        "1. Evaluate the 'traffic_score' (0 to 100 integer) based on its ability to drive organic search traffic and high user engagement on a blog.\n"
        "2. Classify the 'category' (e.g., TECH: AI & Robotics, TECH: Privacy, GAMING: Industry, etc.).\n"
        "3. Write a 'korean_summary' (A concise 2-3 sentence explanation in natural, professional Korean detailing what the discussion is about and why it is trending).\n"
        "4. Generate 'seo_tags' — a list of 5-7 highly specific SEO keyword tags based on the actual content (e.g. ['PlayStation', 'State of Play', 'Sony', 'PS5', 'Gaming Industry']). NO generic tags like 'gaming' or 'tech' alone. ALL tags MUST be in English only. Never use Korean or any other language for tags.\n\n"
        "CRITERIA FOR TRAFFIC SCORE:\n"
        "- High Score (90-100): Mega-trends, industry-shifting news, controversial policies, revolutionary DIY tech/hacks, global product launches.\n"
        "- Low Score (0-89): Normal discussions, minor Q&As, personal bug rants, weekly automated community threads, casual chats.\n\n"
        f"Candidates List (JSON Format):\n{json.dumps(input_package, ensure_ascii=False)}\n\n"
        "Output exactly in this JSON array format. No introductory text, no markdown blocks, just raw JSON:\n"
        '[{"index": 0, "score": 95, "category": "TECH: AI", "korean_summary": "요약문...", "seo_tags": ["AI", "Machine Learning", "OpenAI"]}, ...]'
    )

    filtered_results = []

    try:
        response = client.models.generate_content(
            model=FLASH_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.1
            )
        )

        raw_text = response.text.strip()
        raw_text = re.sub(r'```json|```', '', raw_text).strip()

        json_match = re.search(r'\[.*\]', raw_text, re.DOTALL)
        if not json_match:
            print(f"⚠️ JSON 배열을 찾을 수 없습니다.")
            return filtered_results

        results_list = json.loads(json_match.group())
        results_map = {item['index']: item for item in results_list}

        for idx, cand in enumerate(candidates):
            ai_data = results_map.get(idx, {})
            score = int(ai_data.get("score", ai_data.get("traffic_score", 50)))

            cand['traffic_score'] = score
            cand['assigned_category'] = ai_data.get("category", cand['subreddit'].upper())
            cand['korean_summary'] = ai_data.get("korean_summary", "요약을 가져오지 못했습니다.")
            cand['seo_tags'] = ai_data.get("seo_tags", [cand['subreddit'].lower()])

            if score >= 85:
                print(f"  • [PASS] 점수: {score}점 ➡️ [{cand['subreddit'].upper()}] {cand['title'][:40]}...")
                filtered_results.append(cand)
            else:
                print(f"  • [DROP] 점수: {score}점 ➡️ [{cand['subreddit'].upper()}] {cand['title'][:40]}... (85점 미만 폐기)")

    except Exception as e:
        print(f"⚠️ 통합 원샷 엔진 오류 발생: {e}")
        print("   품질 필터 유지를 위해 해당 사이클을 안전하게 건너뜁니다.")

    return filtered_results


def generate_seo_title(candidate):
    """RSS/Reddit 원본 제목을 블로그용 SEO 제목으로 재생성합니다."""
    original_title = candidate['title']
    # 뉴스RSS는 desc, Reddit은 content를 컨텍스트로 사용 (500자로 확대)
    context = candidate.get('desc', '') or candidate.get('content', '')
    context_snippet = context[:500]

    prompt = (
        f"You are a veteran tech/gaming blog editor. Write a great blog post title.\n\n"
        f"SOURCE HEADLINE: {original_title}\n"
        f"CONTEXT: {context_snippet}\n\n"
        f"RULES:\n"
        f"- Under 60 characters\n"
        f"- Capture the REAL angle (controversy, impact, implication) not just restate the headline\n"
        f"- Punchy and direct — a human editor would approve\n"
        f"- Plain text ONLY: no markdown, no asterisks, no quotes wrapping the title\n"
        f"- Do NOT start with Why/How/What\n"
        f"- Output the title text ONLY. Nothing else."
    )
    try:
        response = client.models.generate_content(
            model=FLASH_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction="You are a concise blog title writer. Output only plain title text. No markdown. No surrounding quotes. No explanation.",
                temperature=0.3,
                max_output_tokens=80
            )
        )
        new_title = response.text.strip()
        # 마크다운/따옴표 잔재 제거
        new_title = re.sub(r'[\*\_`#]', '', new_title).strip()
        new_title = new_title.strip('"').strip("'").strip()
        if new_title and 5 <= len(new_title) <= 70:
            return new_title
    except Exception:
        pass
    # 폴백: 원본 제목 60자 제한
    return original_title if len(original_title) <= 60 else original_title[:57] + "..."


def generate_seo_post(candidate):
    """실제 영미권 베테랑 저널리스트 페르소나로 구글 SEO 문서를 집필합니다."""
    print("🤖 마스터 블러거 페르소나 가동: 영미권 현지 고수 톤으로 SEO 문서 집필 중...")

    system_instruction = (
        "You are a cynical, battle-hardened 10-year veteran tech and gaming journalist. "
        "You've covered E3, GDC, CES. You've seen every hype cycle, every corporate lie, every community meltdown. "
        "Your writing is razor-sharp, occasionally funny, always honest. You write like a human, not a content farm.\n\n"
        "CRITICAL WRITING RULES:\n"
        "1. NEVER use AI filler phrases: 'In the ever-evolving landscape', 'Furthermore', 'In conclusion', "
        "'It's worth noting', 'Needless to say', 'At the end of the day', 'Let's dive in', 'In today's world'.\n"
        "2. NEVER use these overused journalist clichés: 'Let's be clear', 'Here's the thing', 'Make no mistake', "
        "'The bottom line', 'At its core', 'Going forward', 'Moving forward', 'It remains to be seen'.\n"
        "3. Start with a single punchy sentence that makes a bold claim or delivers a verdict. No setup. No context. Just the point.\n"
        "4. Vary sentence length dramatically — mix 3-word punches with longer complex observations. Monotone rhythm = AI tell.\n"
        "5. Structure with clear ## and ### Markdown headings for SEO. Each heading should be specific and searchable.\n"
        "6. Include at least one Markdown Table with real comparative data or analysis.\n"
        "7. Write paragraphs of 2-3 sentences MAX. Mobile readers scroll fast.\n"
        "8. Include dry humor or sarcasm naturally — not forced. Real journalists do this.\n"
        "9. Reference the Reddit community reaction authentically — paraphrase specific types of comments you'd expect to see.\n"
        "10. Add '[!-- ADSENSE_MIDDLE_PLACEHOLDER --]' naturally before the second ## heading.\n"
        "11. FORBIDDEN: emojis in body text.\n"
        "12. FORBIDDEN: 'Conclusion' or 'Summary' headings. End with a punchy, topic-specific ## that delivers a final verdict.\n"
        "13. MANDATORY: minimum 2000 words. Expand with real analysis, historical context, industry implications.\n"
        "14. Include at least one personal observation starting with 'I've seen...' or 'Having covered...' to feel authentic.\n"
        "15. Use rhetorical questions sparingly — max 2-3 in the entire post.\n"
        "16. FORBIDDEN: Do NOT add any 'Source:' line or attribution at the end of the post.\n"
        "17. IMPORTANT: Present all political and policy topics from a balanced, analytical perspective. "
        "Avoid partisan language or ideological labels. Critique ideas on their merits, not their political alignment.\n"
        "18. EXTERNAL LINKS — ONLY IF CERTAIN: If you know a real, verified URL to an authoritative source, include 1-3 links using this format: [anchor text](https://real-url.com). CRITICAL: NEVER fabricate or guess a URL. If the news source URL is provided in the context, you MAY use it directly — that URL is real. Otherwise, only link if 100% certain.\n"
        "19. CRITICAL — NO HALLUCINATION: Do NOT invent, fabricate, or hallucinate ANY specific facts. This includes: product names, game titles, company names, statistics, quotes, event details, or announcements. If the provided context lacks specific details, write in general terms only. NEVER fill gaps with made-up specifics. Every concrete claim must be directly supported by the provided context.\n"
        "20. Output ONLY raw Markdown. No ```markdown blocks. No preamble.\n"
        "21. CRITICAL — WRITE THE FULL ARTICLE: Do not stop mid-sentence or mid-section. Every ## heading must have complete body text. The article must be finished in its entirety before you stop."
    )

    source_type = candidate.get("source", "reddit")
    news_url = candidate.get("news_url", "")

    if source_type == "news" and news_url:
        user_content = (
            f"Source: {news_url}\n"
            f"Headline: {candidate['title']}\n"
            f"Context (news summary + community reaction):\n{candidate['content']}\n\n"
            f"Write a deep analysis of this news story as LIFO. "
            f"The news source URL above is REAL — you may cite it directly as a hyperlink. "
            f"Use the community reaction section to add authentic reader perspective."
        )
    else:
        user_content = (
            f"Source Community: r/{candidate['subreddit']}\n"
            f"Original Topic: {candidate['title']}\n"
            f"Raw community context:\n{candidate['content']}"
        )

    # [FIX] max_output_tokens=8192 추가 — 글 잘림 방지 핵심 수정
    response = client.models.generate_content(
        model=FLASH_MODEL,
        contents=user_content,
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=0.85,
            max_output_tokens=8192
        )
    )

    return response.text


def generate_meta_description(candidate, seo_content):
    """포스트 첫 단락에서 SEO용 메타 디스크립션을 자동 추출합니다."""
    clean = re.sub(r'#+ .*?\n', '', seo_content)
    clean = re.sub(r'\*\*|__|~~|\[.*?\]\(.*?\)', '', clean)
    clean = re.sub(r'\s+', ' ', clean).strip()
    if len(clean) > 155:
        clean = clean[:152] + "..."
    return clean


def build_jekyll_filename(title):
    """Jekyll _posts/ 규격 파일명을 생성합니다: YYYY-MM-DD-slug.md"""
    today = datetime.now().strftime("%Y-%m-%d")
    slug = title.lower()
    slug = re.sub(r'[^\w\s-]', '', slug)
    slug = re.sub(r'[\s_]+', '-', slug)
    slug = re.sub(r'-+', '-', slug).strip('-')
    slug = slug[:60].rstrip('-')
    return f"{today}-{slug}.md"


def build_jekyll_front_matter(candidate, image_data, meta_description, raw_category, seo_title):
    """Minimal Mistakes 테마 규격의 Jekyll front matter를 생성합니다."""

    # 카테고리별 폴백 이미지 (Unsplash 이미지 없을 때)
    fallback_images = {
        "gaming": "https://images.unsplash.com/photo-1493711662062-fa541adb3fc8?w=1080&q=80",
        "tech": "https://images.unsplash.com/photo-1518770660439-4636190af475?w=1080&q=80",
        "technology": "https://images.unsplash.com/photo-1518770660439-4636190af475?w=1080&q=80",
        "software": "https://images.unsplash.com/photo-1555066931-4365d14bab8c?w=1080&q=80",
        "gadgets": "https://images.unsplash.com/photo-1468495244123-6c6c332eeece?w=1080&q=80",
    }

    if image_data:
        header_image = image_data['url']
        photographer = image_data['photographer_name']
    else:
        header_image = fallback_images.get(raw_category, "https://images.unsplash.com/photo-1518770660439-4636190af475?w=1080&q=80")
        photographer = "Unsplash"

    # excerpt 내 따옴표 이스케이프
    safe_excerpt = meta_description.replace('"', "'")
    safe_title = seo_title.replace('"', "'")

    # SEO 태그 생성 (Gemini가 생성한 태그 + 카테고리 기본 태그)
    seo_tags = candidate.get('seo_tags', [])
    if raw_category not in seo_tags:
        seo_tags.insert(0, raw_category)
    tags_str = ', '.join(seo_tags)

    front_matter = f"""---
layout: single
title: "{safe_title}"
date: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")} +0900
categories: [{raw_category}]
tags: [{tags_str}]
excerpt: "{safe_excerpt}"
header:
  image: "{header_image}"
  caption: "Photo by {photographer} on Unsplash"
  teaser: "{header_image}"
author_profile: false
read_time: true
comments: false
share: true
---

"""
    return front_matter


def deploy_to_github(candidate, seo_content):
    """GitHub _posts/ 폴더에 Jekyll 규격 마크다운 파일을 자동 커밋합니다."""

    # 카테고리 변환 (전체 함수에서 공유)
    raw_category = candidate['assigned_category'].split(':')[0].strip().lower()

    # [FIX] SEO 제목을 배포 전에 미리 생성 (파일명 + front matter 모두에 사용)
    seo_title = generate_seo_title(candidate)
    # [FIX] 마크다운 볼드/이탤릭 기호, 따옴표, 콜론 등 front matter 파괴 문자 제거
    seo_title = re.sub(r'[\*\_`#]', '', seo_title)         # 마크다운 기호 제거
    seo_title = seo_title.replace('"', '').strip()            # 큰따옴표만 제거, 아포스트로피 보존
    seo_title = re.sub(r'\s+', ' ', seo_title).strip()      # 연속 공백 정리
    if not seo_title or len(seo_title) < 5:                  # 제목이 날아갔으면 원본 사용
        seo_title = re.sub(r'[\*\_`#]', '', candidate['title']).replace('"', '')[:60]
    print(f"📝 생성된 SEO 제목: {seo_title}")

    # 핵심 키워드 추출 및 Unsplash 이미지 검색
    image_keyword = extract_image_keyword(seo_title)
    image_data = fetch_unsplash_image(image_keyword)

    if image_data:
        print(f"✅ 이미지 확보 완료: 촬영자 - {image_data['photographer_name']}")
    else:
        print("⚠️ 폴백 이미지로 대체합니다.")

    # 메타 디스크립션 자동 생성
    meta_description = generate_meta_description(candidate, seo_content)

    # Jekyll front matter 생성 — seo_title 전달
    front_matter = build_jekyll_front_matter(candidate, image_data, meta_description, raw_category, seo_title)

    # 애드센스 플레이스홀더 교체
    jekyll_content = seo_content.replace(
        "[!-- ADSENSE_MIDDLE_PLACEHOLDER --]",
        "<!-- ADSENSE_MIDDLE_PLACEHOLDER -->"
    )

    # 촬영자 크레딧 본문 최상단 삽입
    if image_data:
        credit_line = (
            f"\n*Photo by [{image_data['photographer_name']}]"
            f"({image_data['photographer_url']}) on "
            f"[Unsplash](https://unsplash.com?utm_source=lifolike&utm_medium=referral)*\n\n"
        )
        final_content = front_matter + credit_line + jekyll_content
    else:
        final_content = front_matter + jekyll_content

    # [FIX] 파일명도 SEO 제목 기반으로 생성 (RSS 원본 제목 아님)
    filename = build_jekyll_filename(seo_title)
    github_path = f"_posts/{filename}"
    slug = re.sub(r'^\d{4}-\d{2}-\d{2}-', '', filename).replace('.md', '')

    # ── 로컬 HTML 프리뷰 병행 생성 ──
    import markdown
    html_body = markdown.markdown(jekyll_content, extensions=['tables', 'fenced_code'])
    html_body = html_body.replace(
        "<!-- ADSENSE_MIDDLE_PLACEHOLDER -->",
        "<div class='adsense-box'>GOOGLE ADSENSE DISPLAY AD PLACEHOLDER</div>"
    )

    image_url = image_data["url"] if image_data else "https://images.unsplash.com/photo-1462331940025-496dfbfc7564?w=1080&q=80"
    image_alt = image_data["alt"] if image_data else image_keyword
    credit_html = (
        f'<p class="photo-credit">Photo by '
        f'<a href="{image_data["photographer_url"]}" target="_blank">{image_data["photographer_name"]}</a> on '
        f'<a href="https://unsplash.com?utm_source=lifolike&utm_medium=referral" target="_blank">Unsplash</a></p>'
    ) if image_data else ""

    html_template = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="description" content="{meta_description}">
    <title>[PREVIEW] {seo_title}</title>
    <style>
        body {{ font-family: 'Malgun Gothic', sans-serif; line-height: 1.7; padding: 40px; background: #f9f9f9; color: #333; }}
        .container {{ max-width: 700px; margin: 0 auto; background: #fff; padding: 30px; border-radius: 8px; box-shadow: 0 4px 10px rgba(0,0,0,0.05); }}
        h1 {{ font-size: 26px; color: #111; border-bottom: 2px solid #eee; padding-bottom: 15px; }}
        h2 {{ font-size: 20px; color: #222; margin-top: 30px; border-left: 4px solid #007bff; padding-left: 10px; }}
        h3 {{ font-size: 17px; color: #444; }}
        .meta {{ color: #777; font-size: 13px; margin-bottom: 20px; }}
        .main-img {{ width: 100%; border-radius: 6px; margin-bottom: 5px; }}
        .photo-credit {{ font-size: 11px; color: #999; text-align: right; margin-bottom: 20px; }}
        .photo-credit a {{ color: #999; text-decoration: none; }}
        .photo-credit a:hover {{ text-decoration: underline; }}
        .adsense-box {{ background: #f0f2f5; padding: 20px; text-align: center; color: #666; font-size: 12px; border: 1px dashed #bbb; margin: 25px 0; font-weight: bold; }}
        table {{ width: 100%; border-collapse: collapse; margin: 20px 0; }}
        th {{ background: #007bff; color: #fff; padding: 10px; text-align: left; }}
        td {{ padding: 10px; border-bottom: 1px solid #eee; }}
        tr:nth-child(even) {{ background: #f9f9f9; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="meta">Category: r/{candidate['subreddit']} | Traffic Score: {candidate.get('traffic_score', 0)}pts | Preview Time: {datetime.now().strftime("%Y-%m-%d %H:%M")}</div>
        <h1>{seo_title}</h1>
        <img class="main-img" src="{image_url}" alt="{image_alt}">
        {credit_html}
        {html_body}
    </div>
</body>
</html>"""

    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html_template)
    print(f"📂 로컬 HTML 프리뷰 생성 완료: {OUTPUT_HTML}")

    # ── GitHub 실제 배포 ──
    print(f"\n🚀 GitHub 배포 시작: {github_path}")
    try:
        auth = Auth.Token(GITHUB_TOKEN)
        g = Github(auth=auth)
        repo = g.get_repo(GITHUB_REPO_NAME)
        commit_message = f"feat: add post - {seo_title[:60]}"

        try:
            existing = repo.get_contents(github_path)
            repo.update_file(
                path=github_path,
                message=commit_message,
                content=final_content,
                sha=existing.sha
            )
            print(f"✅ 기존 파일 업데이트 완료: {github_path}")
        except GithubException:
            repo.create_file(
                path=github_path,
                message=commit_message,
                content=final_content
            )
            print(f"✅ 신규 파일 커밋 완료: {github_path}")

        print(f"\n🎉 GitHub Pages 배포 완료!")
        print(f"🌐 약 1~2분 후 확인: https://blog.lifo-like.com/{raw_category}/")
        return raw_category, slug

    except Exception as e:
        print(f"⚠️ GitHub 배포 실패: {e}")
        print("   로컬 HTML 프리뷰는 정상 생성되었습니다.")
        return raw_category, slug


# =====================================================================
# 🚀 메인 오케스트레이션 엔진 구동
# =====================================================================
if __name__ == "__main__":
    print("======================================================================")
    print("🔥 [INTELLIGENCE GLOBAL GEMINI BOT v4] 가동 시작")
    print("======================================================================\n")

    # ── 하루 1회 발행 안전장치 (UTC 기준) ──
    history = load_history()
    now_utc = datetime.now(timezone.utc)
    today = now_utc.strftime("%Y-%m-%d")
    now_utc_str = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    last_published_date = history.get("last_published_date")

    if last_published_date == today:
        msg = f"오늘({today}) 이미 발행 완료. API 중복 호출 방지를 위해 종료합니다."
        print(f"⚠️ {msg}")
        log_to_github(msg, "INFO")
        exit()

    last_category = history.get("last_category", None)
    print(f"📋 마지막 발행 카테고리: {last_category or '없음 (첫 실행)'}")
    print(f"📅 마지막 발행 날짜: {last_published_date or '없음'}")
    log_to_github(f"봇 시작 — 마지막 발행: {last_published_date or '없음'} / 카테고리: {last_category or '없음'}", "INFO")

    raw_candidates = fetch_global_trends(TARGET_SUBREDDITS, history)

    if not raw_candidates:
        msg = "모든 최신 피드가 이미 처리되었거나 후보군이 비어 있습니다."
        print(f"⚠️ {msg}")
        log_to_github(msg, "WARNING")
        exit()

    print(f"✨ 실시간 수집된 데이터 {len(raw_candidates)}개를 확보했습니다.")

    clean_candidates = evaluate_filter_and_summarize_oneshot(raw_candidates)

    if not clean_candidates:
        msg = "금일 수집된 피드 중 85점을 돌파한 메가 트렌드가 존재하지 않습니다."
        print(f"\n⚠️ [안내] {msg}")
        print("   노이즈 없는 청정 관리를 위해 프로그램을 안전하게 종료합니다.")
        log_to_github(msg, "WARNING")
        exit()

    tech_subs = ["technology", "software", "gadgets"]
    gaming_subs = ["gaming", "games"]

    tech_pool = sorted(
        [c for c in clean_candidates if c['subreddit'] in tech_subs],
        key=lambda x: x['traffic_score'], reverse=True
    )
    gaming_pool = sorted(
        [c for c in clean_candidates if c['subreddit'] in gaming_subs],
        key=lambda x: x['traffic_score'], reverse=True
    )

    print(f"\n✨ 90점 이상 메가 트렌드 — Tech: {len(tech_pool)}개, Gaming: {len(gaming_pool)}개\n")

    selected_post = None

    if AUTO_MODE:
        print("🤖 [AUTO MODE] 카테고리 교차 발행 로직 가동...")

        if last_category == "gaming":
            primary_pool = tech_pool
            fallback_pool = gaming_pool
            print("  • 어제 gaming 발행 → 오늘 tech 우선 선택")
        elif last_category == "tech":
            primary_pool = gaming_pool
            fallback_pool = tech_pool
            print("  • 어제 tech 발행 → 오늘 gaming 우선 선택")
        else:
            primary_pool = sorted(clean_candidates, key=lambda x: x['traffic_score'], reverse=True)
            fallback_pool = []
            print("  • 첫 실행 → 전체 최고 점수 선택")

        if primary_pool:
            selected_post = primary_pool[0]
        elif fallback_pool:
            selected_post = fallback_pool[0]
            print("  • 우선 카테고리 후보 없음 → 폴백 카테고리로 전환")
        else:
            print("⚠️ 선택 가능한 후보가 없습니다.")
            exit()

        print(f"  • 선택된 포스트: [{selected_post['traffic_score']}점] {selected_post['title'][:50]}...")

    else:
        balanced_candidates = tech_pool[:2] + gaming_pool[:2]
        dashboard = {}
        display_idx = 1

        print("📊 [MONITORING DASHBOARD] 트렌드 리포트를 출력합니다...")
        for cand in balanced_candidates:
            print(f"----------------------------------------------------------------------")
            print(f" [{display_idx}] 카테고리: {cand['assigned_category']} (출처: r/{cand['subreddit']} | 가치 점수: {cand['traffic_score']}점)")
            print(f"  • 원문 제목: {cand['title']}")
            print(f"  • 한국어 요약: {cand['korean_summary']}")
            dashboard[str(display_idx)] = cand
            display_idx += 1
        print(f"----------------------------------------------------------------------\n")

        while True:
            choice = input("✍️ 블로그에 배포할 게시글 번호를 입력하세요 (종료: q): ").strip()
            if choice.lower() == 'q':
                print("프로그램을 안전하게 종료합니다.")
                exit()
            if choice in dashboard:
                selected_post = dashboard[choice]
                break
            print("⚠️ 올바른 번호를 선택해 주세요.")

    if selected_post:
        print(f"\n🎯 최종 선택된 주제: {selected_post['title']}")

        seo_article = generate_seo_post(selected_post)
        raw_category, slug = deploy_to_github(selected_post, seo_article)
        blog_url = f"https://blog.lifo-like.com/{raw_category}/{slug}/"

        # history 업데이트 (UTC 기준)
        history["published_ids"].append(selected_post['id'])
        # [FIX] 토픽 중복방지용 제목 저장 — 최근 40개만 유지
        history.setdefault("published_titles", [])
        history["published_titles"].append(selected_post['title'])
        history["published_titles"] = history["published_titles"][-40:]
        # published_ids도 무한 누적 방지 — 최근 200개만 유지
        history["published_ids"] = history["published_ids"][-200:]
        history["last_category"] = raw_category
        history["last_published_date"] = today
        history["last_published_at"] = now_utc_str
        save_history(history)

        success_msg = f"발행 완료 — 제목: {selected_post['title'][:50]} / 카테고리: {raw_category} / URL: {blog_url}"
        log_to_github(success_msg, "SUCCESS")
        print(f"\n🏁 파이프라인 1회 사이클 완료! 발행 카테고리: {raw_category}")
