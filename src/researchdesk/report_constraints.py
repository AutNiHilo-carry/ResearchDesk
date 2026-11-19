"""Deterministic body limits, separate from semantic/model and human judgments."""
import argparse
import json
from pathlib import Path
import re
from .citations import validate_citation_syntax
from .verification import digest

VERSION = 'body-length-v1'
REFERENCE = r'(?:\[E[0-9a-f]{16}\]|［E[0-9a-f]{16}］)'


def policy(max_chars=200):
    if type(max_chars) is not int or not 1 <= max_chars <= 100000:
        raise ValueError('Body limit must be an integer in 1..100000.')
    return dict(version=VERSION, max_chars=max_chars, unit='unicode_codepoints',
                excluded='whitespace_and_canonical_evidence_references', reference_pattern=REFERENCE,
                included='all_other_text_including_markdown_punctuation_and_fenced_content')


def counting_instruction(max_chars=200):
    return (f"正文长度合同 body-length-v1：上限{policy(max_chars)['max_chars']}个 Unicode 码点。"
            "只忽略 Unicode 空白和规范 Evidence 引用（E 后16位小写十六进制，使用成对方括号或全角方括号）。"
            "所有其他字符都计数，包括标点、英文、Markdown、代码围栏及其内容和零宽字符；组合字符可能占多个码点。"
            "正文不能为空。保留任务要求和证据引用，不以删掉业务要求、重复事实或隐藏正文换取长度合规。")


def check_body(report, *, max_chars=200):
    spec = policy(max_chars)
    if not isinstance(report, str):
        raise TypeError('A text body is required.')
    # No markdown stripping, appendix splitting, truncation or report mutation.
    counted = re.sub(r'\s', '', re.sub(REFERENCE, '', report))
    reasons = []
    if not counted:
        reasons.append('empty_counted_body')
    try:
        validate_citation_syntax(report)
    except ValueError:
        reasons.append('malformed_citation_syntax')
    if len(counted) > max_chars:
        reasons.append('body_too_long')
    invalid = any(reason != 'body_too_long' for reason in reasons)
    status = 'invalid' if invalid else 'failed' if reasons else 'passed'
    return dict(policy=spec, body_sha256=digest(report), counted_chars=len(counted),
                status=status, passed=status == 'passed', reasons=reasons,
                scope='body_length_only_not_evidence_integrity_or_semantic_quality', api_calls=0)


def main():
    parser = argparse.ArgumentParser(description='Offline explicit body-length check; no semantic scores')
    parser.add_argument('body', type=Path)
    parser.add_argument('--max-chars', type=int, default=200)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.body.suffix.lower() not in {'.md', '.txt'} or args.output.suffix.lower() != '.json':
        parser.error('Input must be a Markdown/text body; output must be a new JSON file.')
    try:
        result = check_body(args.body.read_text(encoding='utf-8'), max_chars=args.max_chars)
        with args.output.open('x', encoding='utf-8') as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
        print(f"Body constraint {result['status']}: {result['counted_chars']}/{args.max_chars}; {args.output}")
        return 0 if result['passed'] else 2 if result['status'] == 'invalid' else 3
    except Exception as error:
        print(f'Body constraint check rejected ({type(error).__name__}); no semantic score assigned.')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
