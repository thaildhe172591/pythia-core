# pythia-core

[English](README.md) · **Tiếng Việt**

Phần của pythia giống nhau ở mọi database. Các engine package xây trên nó:
[pythia-postgres](https://github.com/thaildhe172591/pythia-postgres),
[pythia-mssql](https://github.com/thaildhe172591/pythia-mssql) và
[pythia-mongo](https://github.com/thaildhe172591/pythia-mongo).

**Alpha (0.1.0).** Những gì đã có:
- engine contract và bộ kiểm tra tuân thủ của nó;
- chọn connection riêng cho từng engine;
- lint cho query contract;
- các lệnh mà command line của mọi engine đều thừa hưởng:
  - `version`, `connections` và `check` (nói rõ khi server cũ hơn phiên bản
    tối thiểu của engine);
  - các lệnh đọc `ls`, `src`, `cols`, `ddl`, `grep`, `deps`, `impact` và
    `errors`, có phân trang và không bao giờ cắt bớt mà không báo;
  - cổng ghi: `apply` (snapshot, rehearse trong một transaction rồi
    rollback — hoặc, với thứ không rehearse được, dự đoán, ghi đúng một lần
    rồi kiểm tra cái đã ghi — và preview), `approve` (của dev: ở console,
    hoặc từ chat qua AskUserQuestion và `approve --hook`, hoặc MCP approver
    `mcp`), `apply --confirm` (chỉ commit đúng thứ đã được duyệt), `policy`,
    `journal list | show | restore` và `history`;
  - `deploy <files>`: một release được rehearse và commit trong một
    transaction, dưới một lần duyệt — vào hết, hoặc không vào gì;
  - `autopilot request | on | off | status | report`: một lease mà dev
    duyệt một lần, chỉ phủ đúng connection, các group và giới hạn ghi trên
    thẻ của nó;
  - `lint <files>` (và `lint --rules`): rule của engine cùng severity và
    quy ước đặt tên của team trên một thay đổi, hiện trong mọi preview;
  - harness: `hook pre-tool` (guard chặn các đường vòng qua cổng và cho qua
    thứ không ghi được), `hook session-start` (luật và trạng thái của
    project), `install` (khung connections, `.gitignore`, hook, MCP approver
    trong file riêng của máy này, khối hướng dẫn, skill — chạy lần hai không
    đổi gì), `doctor` và `skill lint`.

Mỗi cổng chặn được gì, và không chặn được gì, có trong
[SECURITY.md](SECURITY.md). Kế hoạch, và cách mỗi ý tưởng ánh xạ sang từng
database, có trong [DESIGN.md](DESIGN.md).

[pythia](https://github.com/thaildhe172591/pythia) cho Oracle là một công cụ
riêng, tự đứng một mình. Nó không dùng package này và không bị package này
thay đổi.

## Cài đặt

```bash
pip install pythia-core
```

pythia-core đứng một mình thì không có lệnh nào: hãy cài một engine
package — `pythia-postgres`, `pythia-mssql` hoặc `pythia-mongo` — và nó đi
kèm theo. Theo contract, nó không phụ thuộc gì ngoài thư viện chuẩn của
Python. Bản đang phát triển là nhánh main:

```bash
pip install "git+https://github.com/thaildhe172591/pythia-core"
```

## Viết một engine

```python
from pythia_core import cli
from pythia_core.engine import engine_problems

class PostgresEngine:
    name, label, command = "postgres", "PostgreSQL", "pythia-pg"
    env_prefix, version, bind_style = "PYTHIA_PG", "0.1.0", "pyformat"
    # ...the connect tier: driver_errors, connect, session, close,
    # connect_failure, schema_of, target_of, server_version, current_schema
    # ...the read tier: object_names, source, columns (which may name a
    # blind spot, when its columns are inferred), ddl, grep, dependencies,
    # dependents, compile_errors
    # ...and the write tier: classify, prepare_statement, parse_object,
    # destructive_reason, dml_probe, measure_row_set, snapshot, rehearse,
    # restore_statement, privilege_warning
    # ...the predict tier, for statements that cannot be rehearsed:
    # execute, verify_written
    # ...the harness tier: raw_client_pattern, write_sql_pattern,
    # script_pattern, connections_template, skills_dir
    # ...the release tier: split_script
    # ...and the lint tier: lint_rules

assert engine_problems(PostgresEngine(),
                       ("connect", "read", "write", "harness", "release",
                        "lint")) == []

def main(argv=None):
    return cli.main(PostgresEngine(), argv)          # your console script
```

Mỗi engine giữ connection của mình trong `.pythia/<engine>/connections.json`.
Đó không bao giờ là `.pythia/connections.json` của pythia cho Oracle, nên một
project dùng cả hai vẫn an toàn.

## Test (không cần database, không cần driver)

```bash
python tests/test_core.py
python tests/test_cli.py
python tests/test_read.py
python tests/test_gate.py
python tests/test_harness.py
python tests/test_doors.py
python tests/test_release.py
python tests/test_lease.py
python tests/test_lint.py
python tests/test_family.py     # several engines in one project, with two fakes
```

CI chạy chúng trên Ubuntu, Windows và macOS, Python 3.9 và 3.13. Job
`family` của nó còn cài pythia cho Oracle và ba engine vào một project rồi
chạy `tests/family_check.py`: mỗi lần install giữ nguyên hook và khối hướng
dẫn của các bên khác, chạy lần hai không đổi gì, doctor của từng engine đều
qua, và guard cùng answer hook của mỗi engine chỉ hành động cho chính nó.

## Giấy phép

MIT
