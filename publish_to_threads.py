#!/usr/bin/env python3
"""
네이버 블로그 -> Threads 자동 발행 스크립트

동작 방식:
1. 네이버 블로그 RSS 피드에서 최신 글 목록을 가져온다.
2. state/last_published.json 에 저장된 "마지막으로 처리한 글 링크" 목록과 비교해
   아직 Threads에 올리지 않은 새 글만 골라낸다.
3. 새 글 본문을 Groq API로 요약/재구성해서 Threads 글자 수 제한(500자)에 맞춘다.
4. Threads API로 텍스트 게시물을 만든다 (container 생성 -> publish).
5. 처리한 글 링크를 state 파일에 기록해서 중복 게시를 막는다.

필요한 환경변수 (GitHub Actions Secrets로 등록):
  NAVER_BLOG_ID        네이버 블로그 아이디 (blog.naver.com/이 부분)
  THREADS_ACCESS_TOKEN Threads 장기 액세스 토큰
  THREADS_USER_ID       Threads 사용자 ID (숫자, /me 로 조회 가능)
  GROQ_API_KEY          Groq API 키
  GROQ_MODEL            (선택) 기본값 llama-3.3-70b-versatile
"""

import os
import sys
import json
import time
import re
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from html import unescape

STATE_PATH = os.path.join(os.path.dirname(__file__), "state", "last_published.json")
MAX_THREADS_CHARS = 500
THREADS_API_BASE = "https://graph.threads.net/v1.0"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"


def log(msg):
    print(f"[auto-publish] {msg}", flush=True)


def http_request(url, method="GET", data=None, headers=None, timeout=30):
    headers = headers or {}
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"[{method} {url}] HTTP {e.code}: {err_body}") from e


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"seen_links": []}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def strip_html(raw_html):
    text = re.sub(r"<[^>]+>", " ", raw_html or "")
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def fetch_naver_rss(blog_id):
    url = f"https://rss.blog.naver.com/{blog_id}.xml"
    log(f"RSS 가져오는 중: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    root = ET.fromstring(raw)
    items = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        description = strip_html(item.findtext("description") or "")
        pub_date = (item.findtext("pubDate") or "").strip()
        if link:
            items.append({
                "title": title,
                "link": link,
                "description": description,
                "pub_date": pub_date,
            })
    # RSS는 보통 최신순이므로 오래된 것부터 처리하도록 뒤집는다
    items.reverse()
    return items


def summarize_for_threads(title, description, link, groq_api_key, model):
    prompt = (
        "다음은 블로그 글의 제목과 본문 요약입니다. 이 내용을 Threads(스레드)에 올릴 "
        f"홍보 게시물로 재구성해줘. 조건: 500자 이내 한국어, 자연스러운 구어체, "
        "과도한 이모지/해시태그 금지(필요하면 1~2개만), 마지막 줄에 링크를 반드시 포함.\n\n"
        f"제목: {title}\n"
        f"본문 요약: {description[:1500]}\n"
        f"링크: {link}\n"
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "너는 블로그 글을 SNS 게시물로 재구성하는 편집자다."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.7,
        "max_tokens": 400,
    }
    headers = {"Authorization": f"Bearer {groq_api_key}"}
    result = http_request(GROQ_API_URL, method="POST", data=payload, headers=headers)
    text = result["choices"][0]["message"]["content"].strip()

    if link not in text:
        text = text.rstrip()
        reserved = len(link) + 2
        if len(text) + reserved > MAX_THREADS_CHARS:
            text = text[: MAX_THREADS_CHARS - reserved - 1].rstrip() + "…"
        text = f"{text}\n{link}"

    if len(text) > MAX_THREADS_CHARS:
        reserved = len(link) + 2
        body = text[: MAX_THREADS_CHARS - reserved - 1].rstrip()
        if not body.endswith(link):
            body += "…"
        text = f"{body}\n{link}" if not body.endswith(link) else body

    return text


def post_to_threads(user_id, access_token, text):
    # 1) 컨테이너 생성
    create_url = f"{THREADS_API_BASE}/{user_id}/threads"
    create_payload = {
        "media_type": "TEXT",
        "text": text,
        "access_token": access_token,
    }
    create_resp = http_request(create_url, method="POST", data=create_payload)
    creation_id = create_resp.get("id")
    if not creation_id:
        raise RuntimeError(f"컨테이너 생성 실패: {create_resp}")

    # 2) 처리 대기 (Meta 권장: 몇 초 정도 대기 후 publish)
    time.sleep(10)

    # 3) 게시
    publish_url = f"{THREADS_API_BASE}/{user_id}/threads_publish"
    publish_payload = {
        "creation_id": creation_id,
        "access_token": access_token,
    }
    publish_resp = http_request(publish_url, method="POST", data=publish_payload)
    if "id" not in publish_resp:
        raise RuntimeError(f"게시 실패: {publish_resp}")
    return publish_resp["id"]


def main():
    blog_id = os.environ.get("NAVER_BLOG_ID")
    access_token = os.environ.get("THREADS_ACCESS_TOKEN")
    user_id = os.environ.get("THREADS_USER_ID")
    groq_api_key = os.environ.get("GROQ_API_KEY")
    model = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
    max_posts_per_run = int(os.environ.get("MAX_POSTS_PER_RUN", "3"))

    missing = [
        name for name, val in [
            ("NAVER_BLOG_ID", blog_id),
            ("THREADS_ACCESS_TOKEN", access_token),
            ("THREADS_USER_ID", user_id),
            ("GROQ_API_KEY", groq_api_key),
        ] if not val
    ]
    if missing:
        log(f"환경변수 누락: {', '.join(missing)}")
        sys.exit(1)

    state = load_state()
    seen = set(state.get("seen_links", []))

    items = fetch_naver_rss(blog_id)

    if not state.get("bootstrapped"):
        # 첫 실행: 기존에 이미 올라와 있던 글들은 게시 대상에서 제외하고
        # "여기까지는 이미 확인함" 으로만 표시한다. 이렇게 안 하면 블로그에
        # 쌓여 있던 과거 글 전부가 한꺼번에 Threads에 올라가게 된다.
        seen.update(it["link"] for it in items)
        state["seen_links"] = list(seen)
        state["bootstrapped"] = True
        save_state(state)
        log(f"초기 설정 완료: 기존 글 {len(items)}건은 게시 대상에서 제외. "
            f"다음 실행부터 새로 올라오는 글만 자동 게시됩니다.")
        return

    new_items = [it for it in items if it["link"] not in seen]

    if not new_items:
        log("새 글 없음. 종료.")
        return

    log(f"새 글 {len(new_items)}건 발견. 최대 {max_posts_per_run}건 처리.")

    processed = 0
    for item in new_items:
        if processed >= max_posts_per_run:
            break
        try:
            log(f"처리 중: {item['title']}")
            text = summarize_for_threads(
                item["title"], item["description"], item["link"], groq_api_key, model
            )
            post_id = post_to_threads(user_id, access_token, text)
            log(f"게시 완료 (post id: {post_id})")
            seen.add(item["link"])
            processed += 1
        except Exception as e:
            log(f"실패: {item['link']} - {e}")
            # 실패한 글은 seen에 추가하지 않음 -> 다음 실행 때 재시도
            break

    state["seen_links"] = list(seen)
    save_state(state)
    log(f"완료. 이번 실행에서 {processed}건 게시.")


if __name__ == "__main__":
    main()
