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
THREADS_APP_ID = os.getenv("THREADS_APP_ID")
THREADS_APP_SECRET = os.getenv("THREADS_APP_SECRET")
THREADS_ACCESS_TOKEN = os.getenv("THREADS_ACCESS_TOKEN")
THREADS_USER_ID = os.getenv("THREADS_USER_ID")

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
            return {"published_ids": data, "last_category": None, "last_published_date": None}
        return data
    except Exception:
        return {"published_ids": [], "last_category": None, "last_published_date": None}


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


def check_threads_token_expiry(history_data):
    """Threads 토큰 만료 임박 시 GitHub Actions 경고를 출력합니다."""
    last_at = history_data.get("last_published_at")
    token_refreshed_at = history_data.get("threads_token_refreshed_at")

    check_date_str = token_refreshed_at or last_at
    if not check_date_str:
        return

    try:
        check_date = datetime.fromisoformat(check_date_str.replace("Z", "+00:00"))
        now_utc = datetime.now(timezone.utc)
        days_elapsed = (now_utc - check_date).days

        if days_elapsed >= 50:
            warning = f"⚠️ [THREADS TOKEN WARNING] 토큰 발급 후 {days_elapsed}일 경과. 곧 만료됩니다. Meta Developer에서 토큰을 재발급하세요!"
            print(warning)
            print("::warning::" + warning)  # GitHub Actions 경고 표시
            log_to_github(warning, "WARNING")
    except Exception:
        pass


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

        # alt 텍스트를 키워드 포함 서술형으로 개선
        topic_keyword = keyword.split()[:3]
        topic_str = ' '.join(topic_keyword).title()
        if alt and len(alt.strip()) >= 5:
            alt = f"{alt.strip().capitalize()} — {topic_str} coverage on LIFO-LIKE"
        else:
            alt = f"{topic_str} — tech and gaming analysis on LIFO-LIKE"
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


def fetch_global_trends(subreddits, history):
    """지정된 커뮤니티 풀에서 실시간 트렌드 피드를 스캔하고 중복을 제외한 청정 목록을 빌드합니다."""
    print("🛰️ 글로벌 레딧 커뮤니티 네트워크에서 실시간 이슈 스캔 중...")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }
    candidates = []
    now_utc = datetime.now(timezone.utc)

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

                # ── 날짜 필터: 48시간 이내 글만 허용 ──
                updated_tag = entry.find("updated")
                if updated_tag:
                    try:
                        post_time = datetime.fromisoformat(updated_tag.text.replace("Z", "+00:00"))
                        hours_old = (now_utc - post_time).total_seconds() / 3600
                        if hours_old > 48:
                            raw_title = entry.find("title").text if entry.find("title") else ""
                            print(f"  • [SKIP] {hours_old:.0f}시간 전 오래된 글 제외: {raw_title[:40]}...")
                            continue
                    except Exception:
                        pass  # 날짜 파싱 실패 시 통과 허용

                title = entry.find("title").text if entry.find("title") else "No Title"
                title = clean_reddit_title(title)  # Reddit 태그 제거
                content = entry.find("content").text if entry.find("content") else ""
                clean_content = re.sub(r'<[^>]*>', '', content).strip()

                candidates.append({
                    "id": post_id,
                    "subreddit": sub,
                    "title": title,
                    "link": link,
                    "content": clean_content[:1000]
                })
        except Exception as e:
            print(f"⚠️ r/{sub} 데이터 로드 중 일시적 지연 발생: {e}")
            continue

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
        "- High Score (90-100): Mega-trends, industry-shifting news, controversial policies, revolutionary DIY tech/hacks, global product launches. The event or news must be RECENT (within the last 7 days).\n"
        "- Medium Score (70-84): Interesting discussions but not breaking news, or topics that are relevant but not immediately timely.\n"
        "- Low Score (0-69): Normal discussions, minor Q&As, personal bug rants, weekly automated community threads, casual chats.\n"
        "- FRESHNESS PENALTY: If the core news event is older than 2 weeks (e.g. a product launch from last month, a policy change from weeks ago), deduct 20-30 points regardless of topic importance. Reddit may resurface old news; always evaluate the ORIGINAL event date, not the Reddit post date.\n\n"
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


def sanitize_title(title):
    """제목에서 Jekyll/마크다운 렌더링을 망가뜨리는 특수문자를 제거합니다."""
    # 파이프(|), 백틱(`), 꺾쇠(<>) 등 마크다운 표·코드 문법 유발 문자 제거
    title = re.sub(r'[|`<>]', '', title)
    # 연속 공백 정리
    title = re.sub(r'\s+', ' ', title).strip()
    return title


def generate_seo_title(candidate):
    """60자 이내 SEO 최적화 제목을 자동 생성합니다."""
    title = sanitize_title(candidate['title'])
    if len(title) <= 60:
        return title

    prompt = (
        f"Rewrite this title to be under 60 characters for Google SEO. "
        f"Keep the core topic and most important keywords. Be punchy and direct.\n"
        f"Original: {title}\n"
        f"RULES:\n"
        f"- Under 60 characters\n"
        f"- FORBIDDEN: Do NOT use pipe characters (|), backticks, or any markdown syntax\n"
        f"- FORBIDDEN: Do NOT use colons to separate date/subtitle (e.g. 'Title | June 2026' is wrong)\n"
        f"- Output ONLY the new title, nothing else."
    )
    try:
        response = client.models.generate_content(
            model=FLASH_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.3)
        )
        short_title = response.text.strip().strip('"').strip("'")
        if len(short_title) <= 60:
            return short_title
    except Exception:
        pass
    # 폴백: 60자에서 자르기
    return title[:57] + "..."


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
        "18. MANDATORY: Include 2-3 contextual external links to authoritative sources (official company blogs, Reuters, BBC, Ars Technica, The Verge, IGN, etc.) using natural anchor text. Example: [McDonald's official statement](https://url). Do NOT link to Reddit directly.\n"
        "19. Output ONLY raw Markdown. No ```markdown blocks. No preamble."
    )

    user_content = (
        f"Source Community: r/{candidate['subreddit']}\n"
        f"Original Topic: {candidate['title']}\n"
        f"Raw community context:\n{candidate['content']}"
    )

    response = client.models.generate_content(
        model=FLASH_MODEL,
        contents=user_content,
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=0.85
        )
    )

    return response.text


def generate_meta_description(candidate, seo_content):
    """Gemini를 활용해 SERP 클릭률 최적화 메타 디스크립션을 생성합니다."""
    try:
        prompt = (
            f"Write a compelling meta description for this blog post for Google search results.\n"
            f"Topic: {candidate['title']}\n"
            f"RULES:\n"
            f"1. Exactly 120-155 characters (count carefully).\n"
            f"2. Include the core keyword naturally in the first half.\n"
            f"3. Create curiosity or urgency — make people WANT to click.\n"
            f"4. Do NOT start with 'Learn', 'Discover', 'Find out', or 'In this article'.\n"
            f"5. Write in active voice. Be direct and specific.\n"
            f"6. FORBIDDEN: Do NOT use clickbait phrases like 'Don't miss', 'Click here', 'Find out more', 'You won't believe', 'Must read'.\n"
            f"7. Output ONLY the meta description text, nothing else."
        )
        response = client.models.generate_content(
            model=FLASH_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.5)
        )
        desc = response.text.strip().strip('"').strip("'")
        if len(desc) > 160:
            return desc[:157] + "..."
        return desc
    except Exception:
        clean = re.sub(r'#+ .*?\n', '', seo_content)
        clean = re.sub(r'\*\*|__|~~|\[.*?\]\(.*?\)', '', clean)
        clean = re.sub(r'\s+', ' ', clean).strip()
        if len(clean) > 155:
            clean = clean[:152] + "..."
        return clean


def build_jekyll_filename(seo_title):
    """Jekyll _posts/ 규격 파일명을 생성합니다: YYYY-MM-DD-slug.md"""
    today = datetime.now().strftime("%Y-%m-%d")
    slug = seo_title.lower()
    slug = re.sub(r'[^\w\s-]', '', slug)
    slug = re.sub(r'[\s_]+', '-', slug)
    slug = re.sub(r'-+', '-', slug).strip('-')
    # URL 잘림 방지: 단어 단위로 45자 이내 제한
    if len(slug) > 45:
        parts = slug.split('-')
        result = []
        length = 0
        for part in parts:
            if length + len(part) + 1 > 45:
                break
            result.append(part)
            length += len(part) + 1
        slug = '-'.join(result)
    return f"{today}-{slug}.md", slug


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

    # slug 생성 (permalink용)
    _, slug = build_jekyll_filename(seo_title)

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
permalink: /{raw_category}/{slug}/
header:
  image: "{header_image}"
  caption: "Photo by {photographer} on Unsplash"
  teaser: "{header_image}"
author_profile: false
read_time: true
comments: false
share: true
---

<script type="application/ld+json">
{{
  "@context": "https://schema.org",
  "@type": "BlogPosting",
  "headline": "{safe_title}",
  "description": "{safe_excerpt}",
  "image": "{header_image}",
  "datePublished": "{datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}+09:00",
  "dateModified": "{datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}+09:00",
  "author": {{
    "@type": "Person",
    "name": "LIFO",
    "url": "https://blog.lifo-like.com/about/"
  }},
  "publisher": {{
    "@type": "Organization",
    "name": "LIFO-LIKE",
    "logo": {{
      "@type": "ImageObject",
      "url": "https://blog.lifo-like.com/favicon.png"
    }}
  }},
  "mainEntityOfPage": {{
    "@type": "WebPage",
    "@id": "https://blog.lifo-like.com/{raw_category}/{slug}/"
  }}
}}
</script>

"""
    return front_matter


def generate_threads_post(candidate, blog_url):
    """Gemini를 활용해 Threads용 임팩트 있는 영문 포스트를 자동 생성합니다."""
    prompt = (
        f"You are a sharp, witty tech/gaming commentator on Threads (like Twitter).\n"
        f"Based on this topic, write a Threads post that makes people WANT to click the link.\n\n"
        f"Topic: {candidate['title']}\n"
        f"Context: {candidate['content'][:300]}\n"
        f"Blog URL: {blog_url}\n\n"
        f"RULES:\n"
        f"1. Start with a bold, provocative 1-2 sentence hook about the topic itself (NOT about the blog).\n"
        f"2. Add 3 punchy bullet points (→) with the most interesting facts or arguments.\n"
        f"3. End with: 'Full breakdown 👉 {blog_url}'\n"
        f"4. Add 4-5 relevant hashtags based on the topic content (e.g. #Fortnite #UKPolicy #Gaming).\n"
        f"5. Total length: under 500 characters.\n"
        f"6. NO generic phrases like 'Check out my blog' or 'I wrote about'.\n"
        f"7. Output ONLY the post text, nothing else.\n"
        f"8. FORBIDDEN: Do NOT use ANY markdown formatting. This means absolutely no **bold**, no *italic* (not even *single asterisks around words*), no # headers, no ~~strikethrough~~. Plain text ONLY.\n"
        f"9. FORBIDDEN: Do NOT write sentences in ALL CAPS. Normal sentence case only. Emphasis must come from word choice, not capitalization."
    )

    response = client.models.generate_content(
        model=FLASH_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(temperature=0.85)
    )

    return response.text.strip()


def post_to_threads(candidate, blog_url):
    """Threads API를 통해 Gemini가 생성한 임팩트 포스트를 자동 발행합니다."""
    print("\n📱 Threads 자동 포스팅 시작...")

    try:
        # Gemini로 Threads 포스트 생성
        print("  • Gemini로 Threads 포스트 생성 중...")
        thread_text = generate_threads_post(candidate, blog_url)
        print(f"  • 생성된 포스트:\n{thread_text}\n")

        # 토큰 유효성 체크 (자동 갱신 없음 — 만료 시 수동 갱신 필요)
        token = THREADS_ACCESS_TOKEN
        check_url = f"https://graph.threads.net/v1.0/me?fields=id&access_token={token}"
        check_res = requests.get(check_url, timeout=10)
        if check_res.status_code != 200:
            print("  • ⚠️ [토큰 만료] Threads 액세스 토큰이 만료되었습니다.")
            print("  • ⚠️ GitHub Secrets > THREADS_ACCESS_TOKEN을 수동으로 갱신해 주세요.")
            print("  • ⚠️ 갱신 방법: developers.facebook.com → 앱 선택 → Threads API → Access Tokens")
            return

        # Step 1 — 미디어 컨테이너 생성 (APP_ID 대신 USER_ID 사용)
        container_url = f"https://graph.threads.net/v1.0/{THREADS_USER_ID}/threads"
        container_payload = {
            "media_type": "TEXT",
            "text": thread_text,
            "access_token": token
        }
        container_res = requests.post(container_url, data=container_payload, timeout=15)
        container_data = container_res.json()

        if "id" not in container_data:
            print(f"⚠️ Threads 컨테이너 생성 실패: {container_data}")
            return

        container_id = container_data["id"]
        print(f"  • 컨테이너 생성 완료: {container_id}")

        # Step 2 — 게시물 발행
        publish_url = f"https://graph.threads.net/v1.0/{THREADS_USER_ID}/threads_publish"
        publish_payload = {
            "creation_id": container_id,
            "access_token": token
        }
        publish_res = requests.post(publish_url, data=publish_payload, timeout=15)
        publish_data = publish_res.json()

        if "id" in publish_data:
            print(f"✅ Threads 포스팅 완료! ID: {publish_data['id']}")
        else:
            print(f"⚠️ Threads 발행 실패: {publish_data}")

    except Exception as e:
        print(f"⚠️ Threads 포스팅 오류: {e}")


def deploy_to_github(candidate, seo_content):
    """GitHub _posts/ 폴더에 Jekyll 규격 마크다운 파일을 자동 커밋합니다."""

    # 카테고리 변환 (전체 함수에서 공유)
    raw_category = candidate['assigned_category'].split(':')[0].strip().lower()

    # 핵심 키워드 추출 및 Unsplash 이미지 검색
    image_keyword = extract_image_keyword(candidate['title'])
    image_data = fetch_unsplash_image(image_keyword)

    if image_data:
        print(f"✅ 이미지 확보 완료: 촬영자 - {image_data['photographer_name']}")
    else:
        print("⚠️ 폴백 이미지로 대체합니다.")

    # 메타 디스크립션 자동 생성
    meta_description = generate_meta_description(candidate, seo_content)

    # SEO 제목 먼저 생성 (slug에도 사용)
    seo_title = generate_seo_title(candidate)

    # Jekyll front matter 생성
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

    # Jekyll 파일명 생성 (SEO 제목 기반)
    filename, slug = build_jekyll_filename(seo_title)
    github_path = f"_posts/{filename}"

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
    <title>[PREVIEW] {candidate['title']}</title>
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
        <h1>{candidate['title']}</h1>
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
        commit_message = f"feat: add post - {candidate['title'][:60]}"

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

    except Exception as e:
        print(f"⚠️ GitHub 배포 실패: {e}")
        print("   로컬 HTML 프리뷰는 정상 생성되었습니다.")


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

    # ── Threads 토큰 만료 체크 ──
    check_threads_token_expiry(history)

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
        deploy_to_github(selected_post, seo_article)

        # Threads 자동 포스팅
        raw_category = selected_post['assigned_category'].split(':')[0].strip().lower()
        seo_title_for_url = generate_seo_title(selected_post)
        _, slug = build_jekyll_filename(seo_title_for_url)
        blog_url = f"https://blog.lifo-like.com/{raw_category}/{slug}/"
        post_to_threads(selected_post, blog_url)

        # history 업데이트 (UTC 기준)
        history["published_ids"].append(selected_post['id'])
        history["last_category"] = raw_category
        history["last_published_date"] = today
        history["last_published_at"] = now_utc_str
        save_history(history)

        success_msg = f"발행 완료 — 제목: {selected_post['title'][:50]} / 카테고리: {raw_category} / URL: {blog_url}"
        log_to_github(success_msg, "SUCCESS")
        print(f"\n🏁 파이프라인 1회 사이클 완료! 발행 카테고리: {raw_category}")