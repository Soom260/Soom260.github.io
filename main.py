#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""구글 시트(게시 CSV) → index.html 의 '작업 링크' 카드와 '마지막 업데이트' 날짜를 만든다.

흐름: 시트에 적는다 → 게시된 CSV 주소를 읽는다 → index.html 의 주석 표시 사이만 바꿔 쓴다.
표준 라이브러리만 쓴다(csv, urllib, html 등). 바깥에서 받아 쓰는 값은 전부 이스케이프한다.

시트 열 약속: 제목, 내용, 분류, 링크, 공개
  - 제목·내용·공개는 필수, 분류가 비면 '기타', 링크는 http:// 또는 https:// 로 시작
  - 공개 열이 Y 인 행만 화면에 넣는다

데이터가 잘못되면 이유와 행 번호를 출력하고 종료 코드 1 로 멈춘다.
배포가 그 자리에서 중단되므로, 직전에 성공한 화면이 그대로 유지된다.
"""

import argparse
import csv
import html
import io
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

REQUIRED_COLUMNS = ["제목", "내용", "분류", "링크", "공개"]
DEFAULT_CATEGORY = "기타"
KST = timezone(timedelta(hours=9))

# 윈도우 터미널에서도 한글 로그가 깨지지 않게 출력 글자 방식을 UTF-8 로 맞춘다
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

WORKS_START = "<!-- WORKS:START -->"
WORKS_END = "<!-- WORKS:END -->"
UPDATED_START = "<!-- UPDATED:START -->"
UPDATED_END = "<!-- UPDATED:END -->"


def stop(reason, details=()):
    """이유(와 행 번호)를 출력하고 실패로 멈춘다."""
    print("", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    print("[배포 중단] " + reason, file=sys.stderr)
    for line in details:
        print("  - " + line, file=sys.stderr)
    print("시트를 고친 뒤 Actions 에서 Run workflow 를 다시 눌러 주세요.", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    sys.exit(1)


# ----------------------------------------------------------------------
# 1. CSV 읽기
# ----------------------------------------------------------------------
def read_csv_text(csv_path, csv_url):
    """로컬 파일(--csv) 또는 게시 CSV 주소(SHEET_CSV_URL)에서 글자를 읽어 온다."""
    if csv_path:
        if not os.path.exists(csv_path):
            stop("CSV 파일을 찾을 수 없습니다.", ["찾은 경로: " + csv_path])
        with open(csv_path, "rb") as f:
            raw = f.read()
        return raw.decode("utf-8-sig", errors="replace"), "", csv_path

    request = urllib.request.Request(csv_url, headers={"User-Agent": "site-build"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            content_type = response.headers.get("Content-Type", "") or ""
            raw = response.read()
    except Exception as error:  # 주소가 틀렸거나 공개되지 않은 경우
        stop("게시 CSV 주소를 읽지 못했습니다.", ["주소: " + csv_url, "내용: " + str(error)])
    return raw.decode("utf-8-sig", errors="replace"), content_type, csv_url


def check_is_csv(text, content_type, source):
    """CSV 대신 웹페이지(로그인 화면 등)가 온 경우를 걸러낸다."""
    head = text.lstrip()[:500].lower()
    looks_like_page = (
        "text/html" in content_type.lower()
        or head.startswith("<!doctype")
        or head.startswith("<html")
        or "<head" in head
        or "<meta" in head
    )
    if looks_like_page:
        stop(
            "CSV 가 아니라 웹페이지가 왔습니다. 게시된 CSV 주소가 아닙니다.",
            [
                "주소/경로: " + source,
                "서버가 보낸 형식: " + (content_type or "(없음)"),
                "받은 내용 앞부분: " + text.strip()[:120].replace("\n", " "),
                "시트에서 [파일 → 공유 → 웹에 게시] 로 '쉼표로 구분된 값(.csv)' 주소를 받아",
                "  저장소 변수 SHEET_CSV_URL 에 넣어 주세요.",
            ],
        )


# ----------------------------------------------------------------------
# 2. 검사
# ----------------------------------------------------------------------
def parse_rows(text, source):
    reader = csv.DictReader(io.StringIO(text, newline=""))
    field_names = [(name or "").strip() for name in (reader.fieldnames or [])]

    if not field_names:
        stop("CSV 가 비어 있어 열 이름을 찾을 수 없습니다.", ["주소/경로: " + source])

    missing = [name for name in REQUIRED_COLUMNS if name not in field_names]
    if missing:
        stop(
            "시트에 꼭 있어야 하는 열이 없습니다: " + ", ".join(missing),
            [
                "현재 열 이름: " + ", ".join(field_names),
                "필요한 열 이름: " + ", ".join(REQUIRED_COLUMNS),
                "첫 줄의 열 이름을 위와 똑같이 맞춰 주세요.",
            ],
        )

    rows = []
    for offset, raw_row in enumerate(reader):
        # 시트에서는 첫 줄이 열 이름이므로, 데이터는 2행부터다
        row_number = offset + 2
        row = {}
        for key, value in raw_row.items():
            if key is None:
                continue
            row[key.strip()] = (value or "").strip()
        rows.append((row_number, row))
    return rows


def collect_works(rows):
    """공개=Y 행만 모으고, 잘못된 값은 행 번호와 함께 모아 둔다."""
    problems = []
    works = []

    for row_number, row in rows:
        if not any(row.get(name, "") for name in REQUIRED_COLUMNS):
            continue  # 완전히 빈 줄은 그냥 넘긴다

        public = row.get("공개", "")
        if not public:
            problems.append("%d행: '공개' 가 비어 있습니다 (Y 또는 N 을 적어 주세요)" % row_number)
            continue
        if public.upper() != "Y":
            continue  # 공개 N 인 행은 화면에 넣지 않는다

        title = row.get("제목", "")
        body = row.get("내용", "")
        link = row.get("링크", "")
        category = row.get("분류", "") or DEFAULT_CATEGORY

        if not title:
            problems.append("%d행: '제목' 이 비어 있습니다 (공개=Y 행은 제목이 꼭 필요합니다)" % row_number)
        if not body:
            problems.append("%d행: '내용' 이 비어 있습니다 (공개=Y 행은 내용이 꼭 필요합니다)" % row_number)
        if not link:
            problems.append("%d행: '링크' 가 비어 있습니다 (http:// 또는 https:// 로 시작해야 합니다)" % row_number)
        elif not (link.startswith("http://") or link.startswith("https://")):
            problems.append(
                "%d행: '링크' 형식이 잘못되었습니다 → %s (http:// 또는 https:// 로 시작해야 합니다)"
                % (row_number, link)
            )
        elif " " in link:
            problems.append("%d행: '링크' 안에 빈칸이 있습니다 → %s" % (row_number, link))

        works.append(
            {"row": row_number, "title": title, "body": body, "link": link, "category": category}
        )

    if problems:
        stop("시트 내용에 잘못된 곳이 있습니다. (%d건)" % len(problems), problems)

    if not works:
        stop(
            "공개=Y 인 행이 하나도 없습니다.",
            [
                "화면에 보여 줄 작업이 없으면 빈 화면이 올라가므로 멈췄습니다.",
                "보여 줄 행의 '공개' 열에 Y 를 적어 주세요.",
            ],
        )
    return works


# ----------------------------------------------------------------------
# 3. HTML 만들기
# ----------------------------------------------------------------------
def esc(value):
    return html.escape(value, quote=True)


def build_works_html(works, indent="              "):
    lines = []
    for index, work in enumerate(works):
        delay = "%.2fs" % (index * 0.08)
        lines.append('%s<li class="works__item reveal" style="--wd:%s">' % (indent, delay))
        lines.append(
            '%s  <a class="works__card" href="%s" target="_blank" rel="noopener">'
            % (indent, esc(work["link"]))
        )
        lines.append('%s    <span class="works__cat">%s</span>' % (indent, esc(work["category"])))
        lines.append('%s    <span class="works__name">%s</span>' % (indent, esc(work["title"])))
        lines.append('%s    <span class="works__desc">%s</span>' % (indent, esc(work["body"])))
        lines.append('%s    <span class="works__go" aria-hidden="true">↗</span>' % indent)
        lines.append("%s  </a>" % indent)
        lines.append("%s</li>" % indent)
    return "\n".join(lines)


def replace_between(text, start_mark, end_mark, new_inner, output_path):
    start = text.find(start_mark)
    end = text.find(end_mark)
    if start == -1 or end == -1 or end < start:
        stop(
            "index.html 에서 주석 표시를 찾지 못했습니다: %s / %s" % (start_mark, end_mark),
            [
                "고치려던 파일: " + output_path,
                "두 표시가 짝으로 남아 있어야 이 자리만 안전하게 바꿀 수 있습니다.",
            ],
        )
    return text[: start + len(start_mark)] + new_inner + text[end:]


def today_in_korea():
    return datetime.now(KST).strftime("%Y.%m.%d")


# ----------------------------------------------------------------------
# 4. 실행
# ----------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="구글 시트(게시 CSV)를 읽어 index.html 의 작업 링크와 업데이트 날짜를 채운다."
    )
    parser.add_argument("--csv", default=None, help="로컬 CSV 파일 경로 (없으면 SHEET_CSV_URL 을 쓴다)")
    parser.add_argument("--input", default="index.html", help="원본 HTML (기본: index.html)")
    parser.add_argument("--output", default="_site/index.html", help="결과 HTML (기본: _site/index.html)")
    args = parser.parse_args()

    csv_url = os.environ.get("SHEET_CSV_URL", "").strip()
    if not args.csv and not csv_url:
        stop(
            "읽을 CSV 가 없습니다.",
            [
                "로컬에서는 --csv sample.csv 처럼 파일을 지정해 주세요.",
                "Actions 에서는 저장소 변수 SHEET_CSV_URL 이 비어 있는지 확인해 주세요.",
                "  (Settings → Secrets and variables → Actions → Variables)",
            ],
        )

    print("[1/4] CSV 읽기: " + (args.csv if args.csv else "SHEET_CSV_URL"))
    text, content_type, source = read_csv_text(args.csv, csv_url)
    check_is_csv(text, content_type, source)

    print("[2/4] 열 이름과 값 검사")
    rows = parse_rows(text, source)
    works = collect_works(rows)
    print("      공개=Y 행 %d개를 넣습니다: %s" % (len(works), ", ".join(w["title"] for w in works)))

    print("[3/4] HTML 만들기: " + args.input)
    if not os.path.exists(args.input):
        stop("원본 HTML 을 찾을 수 없습니다.", ["찾은 경로: " + args.input])
    with open(args.input, encoding="utf-8") as f:
        page = f.read()

    inner = "\n" + build_works_html(works) + "\n            "
    page = replace_between(page, WORKS_START, WORKS_END, inner, args.output)

    updated = today_in_korea()
    page = replace_between(page, UPDATED_START, UPDATED_END, updated, args.output)
    print("      마지막 업데이트(한국 시간): " + updated)

    print("[4/4] 저장: " + args.output)
    output_dir = os.path.dirname(args.output)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    with open(args.output, "w", encoding="utf-8", newline="") as f:
        f.write(page)

    print("완료했습니다.")


if __name__ == "__main__":
    main()
