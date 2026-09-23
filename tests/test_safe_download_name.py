"""POST /safe-download-name 的测试：编码解析、回退与路径清理。

运行：
    python -m pip install -r requirements-dev.txt
    python -m pytest -q
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def call(content_disposition: str):
    return client.post(
        "/safe-download-name",
        json={"content_disposition": content_disposition},
    )


def expect_name(content_disposition: str) -> dict:
    resp = call(content_disposition)
    assert resp.status_code == 200, resp.text
    return resp.json()


def expect_422(content_disposition: str, code: str | None = None):
    resp = call(content_disposition)
    assert resp.status_code == 422, resp.text
    body = resp.json()
    if code is not None:
        assert body["error"] == code, body
    return body


# ----------------------------- RFC 5987 filename* -----------------------------


def test_filename_star_utf8_chinese():
    body = expect_name("attachment; filename*=UTF-8''%E4%B8%AD%E6%96%87.txt")
    assert body == {"filename": "中文.txt", "source": "filename_star", "sanitized": False}


def test_filename_star_utf8_euro_and_spaces():
    # € 与空格都走百分号编码；语言标签可省略。
    header = "attachment; filename*=utf-8'en'%E2%82%AC%20rates.csv"
    body = expect_name(header)
    assert body["filename"] == "€ rates.csv"
    assert body["source"] == "filename_star"
    assert body["sanitized"] is False


def test_filename_star_case_insensitive_charset_and_hex():
    body = expect_name("attachment; filename*=Utf-8''%7e%61%2Ebin")
    assert body["filename"] == "~a.bin"


def test_filename_star_iso8859_1():
    # 0xFC=ü、0xDF=ß（ISO-8859-1）。
    body = expect_name("attachment; filename*=iso-8859-1''Gr%FC%DFe.txt")
    assert body["filename"] == "Grüße.txt"
    assert body["source"] == "filename_star"


def test_filename_star_unescaped_attr_chars():
    # 不需要百分号编码的 attr-char 原样保留。
    body = expect_name("attachment; filename*=utf-8''report-2026_v1.0.tar.gz")
    assert body["filename"] == "report-2026_v1.0.tar.gz"


def test_filename_star_takes_precedence_over_filename():
    header = 'attachment; filename="ascii-name.bin"; filename*=utf-8\'\'%E6%97%A5%E6%9C%AC.zip'
    body = expect_name(header)
    assert body["filename"] == "日本.zip"
    assert body["source"] == "filename_star"


def test_filename_star_quoted_is_not_ext_value_falls_back():
    # 带引号的 filename* 不是合法 ext-value：回退到 filename。
    header = 'attachment; filename="ok.txt"; filename*="utf-8\'\'bad.bin"'
    body = expect_name(header)
    assert body["filename"] == "ok.txt"
    assert body["source"] == "filename"


def test_filename_star_unknown_charset_falls_back():
    header = 'attachment; filename="fallback.tar"; filename*=utf-16\'\'%FF%FEa'
    body = expect_name(header)
    assert body["filename"] == "fallback.tar"
    assert body["source"] == "filename"


def test_filename_star_bad_percent_encoding_falls_back():
    header = 'attachment; filename="fb.log"; filename*=utf-8\'\'%ZZ%41'
    body = expect_name(header)
    assert body["filename"] == "fb.log"


def test_filename_star_truncated_percent_encoding_falls_back():
    header = 'attachment; filename="fb.log"; filename*=utf-8\'\'a%4'
    assert expect_name(header)["filename"] == "fb.log"


def test_filename_star_invalid_utf8_bytes_falls_back():
    # 0xFF 不是合法 UTF-8 字节。
    header = 'attachment; filename="fb.log"; filename*=utf-8\'\'%FF%FE'
    assert expect_name(header)["filename"] == "fb.log"


def test_filename_star_missing_quote_separators_falls_back():
    header = 'attachment; filename="fb.log"; filename*=utf-8nodelim.txt'
    assert expect_name(header)["filename"] == "fb.log"


def test_filename_star_empty_value_falls_back():
    header = "attachment; filename=\"fb.log\"; filename*=utf-8''"
    assert expect_name(header)["filename"] == "fb.log"


def test_filename_star_illegal_language_tag_falls_back():
    # 语言标签含非法字符（点号）：ext-value 无效，回退 filename。
    header = "attachment; filename=\"fb.log\"; filename*=utf-8'en.US'x.bin"
    assert expect_name(header)["filename"] == "fb.log"


def test_filename_star_literal_non_ascii_in_header_malformed():
    # 未加引号的 token 里直接出现非 ASCII 字节（历史服务端常见错误）：
    # 头部结构非法，直接 422（应由客户端改用 filename* 百分号编码）。
    header = "attachment; filename*=utf-8''中文.bin"
    body = expect_422(header)
    assert body["error"] == "malformed_content_disposition"


def test_filename_star_duplicate_is_ignored_falls_back():
    # 同名参数重复：RFC 6266 要求整个参数名作废。
    header = (
        "attachment; filename=\"dup.bin\""
        "; filename*=utf-8''a.txt; filename*=utf-8''b.txt"
    )
    body = expect_name(header)
    assert body["filename"] == "dup.bin"
    assert body["source"] == "filename"


# ----------------------------- 传统 filename 回退 -----------------------------


def test_filename_token_unquoted():
    body = expect_name("attachment; filename=plain-name_1.0.bin")
    assert body == {"filename": "plain-name_1.0.bin", "source": "filename", "sanitized": False}


def test_filename_quoted_string_with_spaces():
    body = expect_name('attachment; filename="quarterly report 2026.pdf"')
    assert body["filename"] == "quarterly report 2026.pdf"
    assert body["source"] == "filename"


def test_filename_quoted_pair_quote_unescaping():
    # 原始头值为 filename="a\"b.txt"：\" 解码为字面引号（不含路径分隔符）。
    bs, qt = chr(92), chr(34)
    header = f'attachment; filename="a{bs}{qt}b.txt"'
    body = expect_name(header)
    assert body["filename"] == 'a"b.txt'
    assert body["source"] == "filename"
    assert body["sanitized"] is False


def test_filename_quoted_pair_backslash_then_path_stripped():
    # 原始头值为 filename="a\\b.txt"：quoted-pair 解码为 a\b.txt，
    # 路径清理再把反斜杠当分隔符，只保留最后一段。
    bs = chr(92)
    header = f'attachment; filename="a{bs}{bs}b.txt"'
    body = expect_name(header)
    assert body["filename"] == "b.txt"
    assert body["sanitized"] is True


def test_filename_quoted_iso8859_1_bytes():
    # 0xE4=ä、0xFE=þ 作为 obs-text 按 ISO-8859-1 解释。
    body = expect_name('attachment; filename="Gr\xe4\xfe.txt"')
    assert body["filename"] == "Gräþ.txt"


def test_filename_case_insensitive_parameter_name():
    body = expect_name('attachment; FILENAME="upper.txt"')
    assert body["filename"] == "upper.txt"


def test_inline_disposition_type_accepted():
    body = expect_name('inline; filename="page.html"')
    assert body["filename"] == "page.html"


def test_extension_disposition_type_accepted():
    body = expect_name('form-data; name="file"; filename="upload.bin"')
    assert body["filename"] == "upload.bin"


def test_unknown_parameters_ignored():
    body = expect_name(
        'attachment; size=42; creation-date="Wed, 12 Feb 2026 10:00:00 GMT"; '
        'filename="kept.dat"'
    )
    assert body["filename"] == "kept.dat"


def test_ows_around_separators():
    body = expect_name('attachment ;\tfilename\t=\t"spaced.dat"\t')
    assert body["filename"] == "spaced.dat"


def test_filename_duplicate_is_ignored_and_star_wins():
    header = (
        "attachment; filename=\"first.txt\"; filename=\"second.txt\""
        "; filename*=utf-8''star.txt"
    )
    body = expect_name(header)
    assert body["filename"] == "star.txt"
    assert body["source"] == "filename_star"


# ----------------------------- 路径与名称清理 -----------------------------


def test_strip_relative_posix_path_prefix():
    body = expect_name('attachment; filename="../docs/../../etc/passwd"')
    assert body["filename"] == "passwd"
    assert body["sanitized"] is True


def test_strip_absolute_posix_path_prefix():
    body = expect_name('attachment; filename="/var/log/app.log"')
    assert body["filename"] == "app.log"
    assert body["sanitized"] is True


def test_strip_windows_absolute_path_in_filename_star():
    # 编码后的反斜杠不会被 quoted-pair 吃掉，按路径分隔符处理。
    header = "attachment; filename*=utf-8''C%3A%5CWindows%5CSystem32%5Ccmd.exe"
    body = expect_name(header)
    assert body["filename"] == "cmd.exe"
    assert body["sanitized"] is True


def test_strip_windows_path_with_forward_slash_and_drive():
    body = expect_name('attachment; filename="C:/Users/Admin/Documents/x.docx"')
    assert body["filename"] == "x.docx"
    assert body["sanitized"] is True


def test_strip_unc_path():
    body = expect_name('attachment; filename*=utf-8\'\'%5C%5Cserver%5Cshare%5Cfile.bin')
    assert body["filename"] == "file.bin"


def test_encoded_slash_in_filename_star_is_stripped():
    header = "attachment; filename*=utf-8''..%2F..%2Fsecrets%2Fkey.pem"
    body = expect_name(header)
    assert body["filename"] == "key.pem"
    assert body["sanitized"] is True


def test_control_characters_removed():
    # NUL、tab、BEL、CRLF 的百分号编码均应被剔除。
    header = "attachment; filename*=utf-8''a%00b%09c%07d%0Ae.bin"
    body = expect_name(header)
    assert body["filename"] == "abcde.bin"
    assert body["sanitized"] is True


def test_obs_text_control_characters_in_quoted_filename_cleaned():
    # obs-text 中的 C1 控制字符（0x85 NEL、0x9F APC）允许通过结构解析，
    # 但必须在名称清理阶段删除。
    body = expect_name('attachment; filename="x\x85y\x9f.txt"')
    assert body["filename"] == "xy.txt"
    assert body["sanitized"] is True


def test_del_character_inside_quoted_string_malformed():
    # DEL (0x7f) 不属于 qdtext 也不属于 obs-text：结构非法。
    body = expect_422('attachment; filename="x\x7fy.txt"')
    assert body["error"] == "malformed_content_disposition"


def test_surrounding_whitespace_trimmed():
    body = expect_name('attachment; filename="  notes.txt  "')
    assert body["filename"] == "notes.txt"
    assert body["sanitized"] is True


def test_dot_filename_cleaned_to_dotdotdot_still_valid():
    # 三个点既不是 "." 也不是 ".."，保留为合法文件名。
    body = expect_name('attachment; filename="..."')
    assert body["filename"] == "..."
    assert body["sanitized"] is False


def test_bare_dot_after_path_stripped_is_invalid():
    body = expect_422('attachment; filename="/some/dir/"')
    assert body["error"] == "no_usable_filename"


def test_dotdot_filename_is_invalid():
    body = expect_422('attachment; filename=".."')
    assert body["error"] == "no_usable_filename"


def test_bare_dot_filename_is_invalid():
    assert expect_422('attachment; filename="."')["error"] == "no_usable_filename"


def test_only_control_characters_after_cleanup_invalid():
    header = "attachment; filename*=utf-8''%00%01%02"
    assert expect_422(header)["error"] == "no_usable_filename"


def test_whitespace_only_filename_invalid():
    assert expect_422('attachment; filename="   "')["error"] == "no_usable_filename"


def test_filename_star_unclean_but_filename_clean_falls_back():
    # filename* 解码后只是路径（清理为空/点），回退到 filename。
    header = 'attachment; filename="good.bin"; filename*=utf-8\'\'%2F%2E%2E'
    body = expect_name(header)
    assert body["filename"] == "good.bin"
    assert body["source"] == "filename"


def test_drive_relative_path_stripped():
    # 正斜杠版本的 "C:evil.txt"（盘符前缀直接剥离）。
    body = expect_name('attachment; filename="C:start.bat"')
    assert body["filename"] == "start.bat"
    assert body["sanitized"] is True


def test_bare_drive_letter_is_invalid():
    body = expect_422('attachment; filename="C:"')
    assert body["error"] == "no_usable_filename"


# ----------------------------- 头部结构错误 422 -----------------------------


def test_no_filename_parameters():
    body = expect_422("attachment")
    assert body["error"] == "no_usable_filename"
    assert body["detail"][0]["loc"] == "content_disposition"


def test_missing_disposition_type():
    body = expect_422('; filename="x.bin"')
    assert body["error"] == "malformed_content_disposition"


def test_empty_header_rejected_by_validation():
    resp = call("")
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_request"


def test_missing_field_rejected():
    resp = client.post("/safe-download-name", json={})
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_request"


def test_wrong_type_rejected():
    resp = client.post("/safe-download-name", json={"content_disposition": 123})
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_request"


def test_cr_lf_rejected_header_injection():
    body = expect_422('attachment; filename="x.bin"\r\nX-Injected: 1')
    assert body["error"] == "malformed_content_disposition"


def test_bare_lf_rejected():
    body = expect_422('attachment; filename="x.bin"\nEvil: 1')
    assert body["error"] == "malformed_content_disposition"


def test_unterminated_quoted_string():
    body = expect_422('attachment; filename="unterminated')
    assert body["error"] == "malformed_content_disposition"


def test_bare_quote_inside_unquoted_value():
    body = expect_422('attachment; filename=bad"quote.bin')
    assert body["error"] == "malformed_content_disposition"


def test_space_in_unquoted_value():
    body = expect_422("attachment; filename=bad name.bin")
    assert body["error"] == "malformed_content_disposition"


def test_invalid_parameter_name():
    body = expect_422('attachment; "badname"="x"')
    assert body["error"] == "malformed_content_disposition"


def test_stray_bytes_after_parameter():
    body = expect_422('attachment; filename="x.bin" junk')
    assert body["error"] == "malformed_content_disposition"


def test_trailing_semicolon_empty_parameter():
    body = expect_422('attachment; filename="x.bin";')
    assert body["error"] == "malformed_content_disposition"


def test_missing_equals():
    body = expect_422("attachment; filename")
    assert body["error"] == "malformed_content_disposition"


def test_bad_quoted_pair_control_char():
    body = expect_422('attachment; filename="bad\\\x00"')
    assert body["error"] == "malformed_content_disposition"


def test_both_filenames_duplicated_no_usable_name():
    header = (
        "attachment; filename=\"a\"; filename=\"b\""
        "; filename*=utf-8''a; filename*=utf-8''b"
    )
    body = expect_422(header)
    assert body["error"] == "no_usable_filename"


def test_too_long_header_rejected():
    resp = call("attachment; filename=" + "a" * 9000)
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_request"


def test_header_near_length_limit_accepted():
    name = "a" * 200
    resp = call(f"attachment; filename={name}")
    assert resp.status_code == 200
    assert resp.json()["filename"] == name


def test_semicolon_inside_quoted_string_is_part_of_name():
    body = expect_name('attachment; filename="report;final,v2.pdf"')
    assert body["filename"] == "report;final,v2.pdf"


def test_equals_inside_quoted_string_is_part_of_name():
    body = expect_name('attachment; filename="a=b=c.txt"')
    assert body["filename"] == "a=b=c.txt"


def test_empty_unquoted_value_malformed():
    body = expect_422("attachment; filename=; size=1")
    assert body["error"] == "malformed_content_disposition"


def test_mixed_separators_collapse_to_last_segment():
    # quoted-pair 转义后的反斜杠与正斜杠都按路径分隔符处理。
    body = expect_name('attachment; filename="../a/b\\..\\c/last.dump"')
    assert body["filename"] == "last.dump"
    assert body["sanitized"] is True


def test_null_byte_percent_encoded_removed():
    header = "attachment; filename*=utf-8''evil%00.exe"
    body = expect_name(header)
    assert body["filename"] == "evil.exe"
    assert "\x00" not in body["filename"]


def test_trailing_slash_only_invalid():
    header = "attachment; filename*=utf-8''a%2Fb%2F"
    assert expect_422(header)["error"] == "no_usable_filename"


def test_dotdot_via_star_path_prefix_stripped():
    header = "attachment; filename*=utf-8''%2E%2E%2Fsecret.key"
    body = expect_name(header)
    assert body["filename"] == "secret.key"
    assert body["sanitized"] is True


def test_star_valid_with_filename_present_star_wins():
    # filename* 合法且无需清理：绝不因为同时存在 filename 而改变结果。
    header = 'attachment; filename*=utf-8\'\'star.bin; filename="other.bin"'
    body = expect_name(header)
    assert (body["filename"], body["source"]) == ("star.bin", "filename_star")


def test_star_invalid_charset_but_quoted_filename_used():
    header = 'attachment; filename*=utf-7\'\'x; filename="q.dat"'
    body = expect_name(header)
    assert (body["filename"], body["source"]) == ("q.dat", "filename")


def test_tab_in_ows_positions_allowed():
    body = expect_name('attachment;\tfilename\t=\t"tab.dat"')
    assert body["filename"] == "tab.dat"


# ----------------------------- 响应契约 -----------------------------


def test_response_shape_and_docs():
    body = expect_name('attachment; filename="ok.txt"')
    assert set(body) == {"filename", "source", "sanitized"}
    assert isinstance(body["sanitized"], bool)


def test_endpoint_registered_in_openapi():
    schema = client.get("/openapi.json").json()
    assert "/safe-download-name" in schema["paths"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
