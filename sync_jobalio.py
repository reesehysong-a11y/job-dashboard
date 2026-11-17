#!/usr/bin/env python3
"""
JobFlow 대시보드 전용 잡알리오(https://job.alio.go.kr/recruit.do) 자동 동기화 스크립트
- 매일 아침 9시(KST) GitHub Actions에서 실행되어 맞춤 필터 조건의 공고를 수집합니다.
- 접수 마감일이 실행일(오늘) 기준 최소 1개월(30일) 이후인 공고만 선별합니다.
- 수집 결과를 jobalio_auto.json으로 저장하고, cloud_data.json의 jobs 배열에 신규 공고를 병합합니다.
  (사용자가 이미 수정했거나 삭제한 공고는 덮어쓰지 않고 보존합니다.)
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

BASE_URL = "https://job.alio.go.kr"
RECRUIT_LIST_URL = f"{BASE_URL}/recruit.do"
RECRUIT_VIEW_URL = f"{BASE_URL}/recruitview.do"
KST = dt.timezone(dt.timedelta(hours=9))

# 사용자가 지정한 맞춤 필터 옵션 (스크린샷 기준)
FILTER_OPTIONS = {
    "min_days_remaining": 30,  # 접수 마감일이 현재 기준 최소 30일(1개월) 이후
    "search_lookback_days": 300,
    "regions": ["대구광역시", "부산광역시", "울산광역시", "경상북도", "경상남도"],
    "work_fields": [
        "일반직",
        "행정직",
        "경영직",
        "사무직",
        "관리직",
        "연구직",
        "운영직",
        "전문직",
        "기타",
    ],
    "emp_types": ["정규직"],
    "recruit_types": ["경력", "신입+경력"],
    "edu_levels": ["대졸(4년)", "석사", "박사"],
    "edu_condition": "OR",  # 중복(OR)
    "only_ongoing": True,
    "max_pages": 8,
}

# 내장 Fallback 코드표
FALLBACK_CODES: Dict[str, Tuple[str, str]] = {
    "대구광역시": ("area", "R3014"),
    "부산광역시": ("area", "R3012"),
    "울산광역시": ("area", "R3016"),
    "경상북도": ("area", "R3021"),
    "경상남도": ("area", "R3022"),
    "정규직": ("emp_type", "R1010"),
    "경력": ("rec_type", "R1060"),
    "신입+경력": ("rec_type", "R1070"),
    "대졸(4년)": ("edu_info", "R7040"),
    "석사": ("edu_info", "R7050"),
    "박사": ("edu_info", "R7060"),
}


def normalize_label(text: str) -> str:
    return re.sub(r"\s+", "", text or "").strip()


def extract_form_params_from_dom(soup: BeautifulSoup) -> List[Tuple[str, str]]:
    """잡알리오 검색 페이지 DOM에서 체크박스/라디오 라벨에 매칭되는 (name, value) 쌍을 동적으로 추출합니다."""
    target_labels = set(
        normalize_label(lbl)
        for lbl in (
            FILTER_OPTIONS["regions"]
            + FILTER_OPTIONS["work_fields"]
            + FILTER_OPTIONS["emp_types"]
            + FILTER_OPTIONS["recruit_types"]
            + FILTER_OPTIONS["edu_levels"]
        )
    )
    target_labels.add(
        normalize_label("중복(OR)" if FILTER_OPTIONS["edu_condition"] == "OR" else "단일(AND)")
    )
    if FILTER_OPTIONS["only_ongoing"]:
        target_labels.add(normalize_label("진행중"))

    inputs_by_id: Dict[str, Tag] = {}
    for inp in soup.find_all("input"):
        inp_id = inp.get("id")
        if inp_id:
            inputs_by_id[inp_id] = inp

    extracted: List[Tuple[str, str]] = []
    seen_pairs = set()

    def add_input(inp_tag: Tag) -> None:
        name = inp_tag.get("name")
        value = inp_tag.get("value")
        if name and value:
            pair = (str(name), str(value))
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                extracted.append(pair)

    for label in soup.find_all("label"):
        lbl_text = normalize_label(label.get_text())
        if lbl_text in target_labels:
            for_attr = label.get("for")
            if for_attr and for_attr in inputs_by_id:
                add_input(inputs_by_id[for_attr])
                continue
            nested_input = label.find("input")
            if nested_input:
                add_input(nested_input)
                continue
            prev_input = label.find_previous_sibling("input")
            if prev_input:
                add_input(prev_input)

    for inp in soup.find_all("input", attrs={"type": re.compile(r"checkbox|radio", re.I)}):
        next_text = ""
        if inp.next_sibling and isinstance(inp.next_sibling, str):
            next_text = normalize_label(inp.next_sibling)
        elif inp.find_next_sibling("label"):
            next_text = normalize_label(inp.find_next_sibling("label").get_text())
        if next_text and next_text in target_labels:
            add_input(inp)

    return extracted


def build_search_params(
    soup: Optional[BeautifulSoup], page_no: int, today: dt.date
) -> List[Tuple[str, str]]:
    params: List[Tuple[str, str]] = [("pageNo", str(page_no))]

    start_date = today - dt.timedelta(days=FILTER_OPTIONS["search_lookback_days"])
    end_date = today + dt.timedelta(days=365)

    s_date_name, e_date_name, date_format = "s_date", "e_date", "%Y.%m.%d"
    if soup is not None:
        for inp in soup.find_all("input", attrs={"type": "text"}):
            val = inp.get("value", "")
            name = inp.get("name", "")
            if re.match(r"^\d{4}[.-]\d{2}[.-]\d{2}$", val):
                if "-" in val:
                    date_format = "%Y-%m-%d"
                if "s_" in name or "start" in name.lower():
                    s_date_name = name
                elif "e_" in name or "end" in name.lower():
                    e_date_name = name

    params.append((s_date_name, start_date.strftime(date_format)))
    params.append((e_date_name, end_date.strftime(date_format)))

    dom_params = extract_form_params_from_dom(soup) if soup else []
    if dom_params:
        params.extend(dom_params)
    else:
        for label in (
            FILTER_OPTIONS["regions"]
            + FILTER_OPTIONS["emp_types"]
            + FILTER_OPTIONS["recruit_types"]
            + FILTER_OPTIONS["edu_levels"]
        ):
            if label in FALLBACK_CODES:
                params.append(FALLBACK_CODES[label])
        if FILTER_OPTIONS["only_ongoing"]:
            params.append(("ing", "2"))

    return params


def parse_date_string(text: str, reference_year: int) -> Optional[dt.date]:
    if not text:
        return None
    full_matches = re.findall(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})", text)
    if full_matches:
        y, m, d = full_matches[-1]
        try:
            return dt.date(int(y), int(m), int(d))
        except ValueError:
            pass
    short_matches = re.findall(r"(?<!\d)(\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})(?!\d)", text)
    if short_matches:
        y, m, d = short_matches[-1]
        try:
            return dt.date(2000 + int(y), int(m), int(d))
        except ValueError:
            pass
    return None


def extract_idx_and_url(a_tag: Optional[Tag]) -> Tuple[str, str]:
    if a_tag is None:
        return "", RECRUIT_LIST_URL
    href = a_tag.get("href", "") or ""
    onclick = a_tag.get("onclick", "") or ""
    if "idx=" in href:
        full_url = urljoin(BASE_URL, href)
        parsed = parse_qs(urlparse(full_url).query)
        idx = parsed.get("idx", [""])[0]
        return idx, full_url
    combined = f"{href} {onclick}"
    num_match = re.search(r"(?:idx[=:'\"\s]+|view\s*\(\s*['\"]?)(\d{4,10})", combined, re.I)
    if num_match:
        idx = num_match.group(1)
        return idx, f"{RECRUIT_VIEW_URL}?idx={idx}"
    generic_num = re.search(r"\(\s*['\"]?(\d{5,9})['\"]?\s*\)", combined)
    if generic_num:
        idx = generic_num.group(1)
        return idx, f"{RECRUIT_VIEW_URL}?idx={idx}"
    if href and not href.lower().startswith("javascript"):
        return "", urljoin(BASE_URL, href)
    return "", RECRUIT_LIST_URL


def parse_postings_from_table(
    soup: BeautifulSoup, today: dt.date
) -> List[Dict[str, Any]]:
    now_iso = dt.datetime.now(KST).isoformat()
    results: List[Dict[str, Any]] = []

    for table in soup.find_all("table"):
        headers = [normalize_label(th.get_text()) for th in table.find_all("th")]
        if not headers:
            continue
        if not any(any(k in h for k in ("공고명", "제목", "기관")) for h in headers):
            continue

        tbody = table.find("tbody") or table
        for tr in tbody.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 4:
                continue
            row_text = normalize_label(tr.get_text())
            if "데이터가없습니다" in row_text or "검색결과가없습니다" in row_text:
                continue

            cell_texts = [re.sub(r"\s+", " ", td.get_text(strip=True)) for td in tds]
            a_tag: Optional[Tag] = None
            for td in tds:
                cand = td.find("a")
                if cand and len(cand.get_text(strip=True)) >= 4:
                    a_tag = cand
                    break
            if a_tag is None:
                continue

            title = re.sub(r"\s+", " ", a_tag.get_text(strip=True))
            idx, url = extract_idx_and_url(a_tag)

            org_name, region, emp_type, reg_date_str = "", "", "정규직", ""
            deadline_date: Optional[dt.date] = None

            for col_idx, val in enumerate(cell_texts):
                col_header = headers[col_idx] if col_idx < len(headers) else ""
                if "기관" in col_header and not org_name:
                    org_name = val
                elif any(k in col_header for k in ("근무지", "지역")) and not region:
                    region = val
                elif any(k in col_header for k in ("고용형태", "채용구분", "고용")) and val:
                    emp_type = val
                elif "등록일" in col_header and "마감" not in col_header:
                    reg_date_str = val
                elif any(k in col_header for k in ("마감일", "접수기간", "공고기간", "기한", "마감")):
                    d = parse_date_string(val, today.year)
                    if d:
                        deadline_date = d

            if deadline_date is None:
                found_dates = [
                    d
                    for val in cell_texts
                    if (d := parse_date_string(val, today.year)) is not None
                ]
                if found_dates:
                    deadline_date = max(found_dates)
                    if len(found_dates) >= 2:
                        reg_date_str = min(found_dates).strftime("%Y-%m-%d")

            if deadline_date is None:
                continue

            if not org_name and len(cell_texts) >= 3:
                org_name = cell_texts[2] if cell_texts[2] != title else cell_texts[1]

            days_remaining = (deadline_date - today).days
            if days_remaining < FILTER_OPTIONS["min_days_remaining"]:
                continue

            if region:
                short_regions = ["대구", "부산", "울산", "경북", "경상북도", "경남", "경상남도", "전국"]
                if not any(r in region for r in short_regions):
                    continue

            reg_parsed = parse_date_string(reg_date_str, today.year) or today
            safe_id = f"jobalio-{idx}" if idx else f"jobalio-{ abs(hash(org_name + title)) % 10000000 }"

            # JobFlow 대시보드 호환 스키마로 변환
            job_entry = {
                "id": safe_id,
                "company": org_name,
                "position": title,
                "url": url,
                "startDate": f"{reg_parsed.strftime('%Y-%m-%d')}T09:00:00",
                "deadline": f"{deadline_date.strftime('%Y-%m-%d')}T18:00:00",
                "status": "서류 준비중",
                "category": f"잡알리오 맞춤수집 ({emp_type})",
                "location": region or "대구/부산/울산/경북/경남",
                "source": "jobalio-auto",
                "createdAt": now_iso,
                "requiredDocuments": [
                    {"name": "NCS 기반 입사지원서", "checked": False},
                    {"name": "직무수행계획서 / 경력기술서", "checked": False},
                    {"name": "학위증명서 및 경력증명서", "checked": False},
                    {"name": "블라인드 채용 확인서 및 개인정보동의서", "checked": False},
                ],
                "essayQuestions": [
                    {
                        "question": f"1. [지원동기 및 입사 후 포부] {org_name}에 지원하게 된 동기와 입사 후 달성하고자 하는 목표를 기술해 주십시오.",
                        "limit": 1000,
                        "content": "",
                    },
                    {
                        "question": "2. [직무역량 및 경력기술] 지원 분야와 관련된 주요 연구·업무 경력 및 직무수행계획을 구체적으로 기술해 주십시오.",
                        "limit": 1000,
                        "content": "",
                    },
                ],
            }
            results.append(job_entry)

    return results


def fetch_jobalio_jobs() -> List[Dict[str, Any]]:
    today = dt.datetime.now(KST).date()
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer": RECRUIT_LIST_URL,
        }
    )

    try:
        init_resp = session.get(RECRUIT_LIST_URL, timeout=20)
        init_resp.raise_for_status()
        init_soup = BeautifulSoup(init_resp.text, "html.parser")
    except Exception as exc:
        logger.warning("잡알리오 초기 페이지 접속 실패: %s", exc)
        return []

    collected: Dict[str, Dict[str, Any]] = {}
    for page_no in range(1, FILTER_OPTIONS["max_pages"] + 1):
        params = build_search_params(init_soup, page_no, today)
        try:
            resp = session.get(RECRUIT_LIST_URL, params=params, timeout=20)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("페이지 %d 조회 실패: %s", page_no, exc)
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        page_items = parse_postings_from_table(soup, today)
        if not page_items:
            break

        new_count = 0
        for item in page_items:
            if item["id"] not in collected:
                collected[item["id"]] = item
                new_count += 1
        if new_count == 0:
            break
        time.sleep(0.7)

    jobs = list(collected.values())
    jobs.sort(key=lambda j: j["deadline"])
    return jobs


def sync_dashboard_files(repo_dir: Path) -> None:
    now_str = dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")
    scraped_jobs = fetch_jobalio_jobs()
    logger.info("수집된 맞춤 공고 (마감 30일 이상 여유): %d건", len(scraped_jobs))

    # 1. jobalio_auto.json 저장 (프론트엔드 직접 폴백 및 상태 표시용)
    auto_feed_path = repo_dir / "jobalio_auto.json"
    auto_payload = {
        "updatedAt": now_str,
        "filterSummary": {
            "minDaysRemaining": FILTER_OPTIONS["min_days_remaining"],
            "regions": FILTER_OPTIONS["regions"],
            "workFields": FILTER_OPTIONS["work_fields"],
            "empTypes": FILTER_OPTIONS["emp_types"],
            "recruitTypes": FILTER_OPTIONS["recruit_types"],
            "eduLevels": FILTER_OPTIONS["edu_levels"],
            "eduCondition": FILTER_OPTIONS["edu_condition"],
        },
        "count": len(scraped_jobs),
        "jobs": scraped_jobs,
    }
    auto_feed_path.write_text(
        json.dumps(auto_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("jobalio_auto.json 저장 완료 (%d건)", len(scraped_jobs))

    # 2. cloud_data.json에 신규 공고 안전 병합
    cloud_path = repo_dir / "cloud_data.json"
    if not cloud_path.exists():
        return

    try:
        cloud_data = json.loads(cloud_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("cloud_data.json 파싱 오류: %s", exc)
        return

    existing_jobs: List[Dict[str, Any]] = cloud_data.get("jobs", [])
    existing_ids = {j.get("id") for j in existing_jobs if j.get("id")}
    deleted_ids = set(cloud_data.get("deletedJobIds", []))
    seen_ids = set(cloud_data.get("seenJobalioIds", []))

    added_count = 0
    for job in scraped_jobs:
        jid = job["id"]
        if jid in existing_ids or jid in deleted_ids or jid in seen_ids:
            continue
        existing_jobs.append(job)
        existing_ids.add(jid)
        seen_ids.add(jid)
        added_count += 1

    cloud_data["jobs"] = existing_jobs
    cloud_data["seenJobalioIds"] = sorted(seen_ids)
    cloud_data["jobalioUpdatedAt"] = now_str

    if added_count > 0:
        cloud_data["updatedAt"] = dt.datetime.now(dt.timezone.utc).isoformat()
        cloud_path.write_text(
            json.dumps(cloud_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("cloud_data.json에 신규 공고 %d건을 자동 등록했습니다.", added_count)
    else:
        logger.info("추가할 신규 공고가 없어 기존 cloud_data.json을 유지합니다.")


if __name__ == "__main__":
    sync_dashboard_files(Path(__file__).resolve().parent)
