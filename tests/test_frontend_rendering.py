import subprocess
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "web" / "static" / "prototype.html"


def run_frontend_render_check() -> str:
    script = r"""
const fs = require("fs");
const html = fs.readFileSync("web/static/prototype.html", "utf8");
const match = html.match(/<script>([\s\S]*)<\/script>/);
if (!match) throw new Error("script block not found");
const source = match[1];
const start = source.indexOf("const mdPlaceholders=[];");
const end = source.indexOf("async function respondReal");
if (start < 0 || end < 0) throw new Error("renderer slice not found");
const esc = s => String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/"/g,"&quot;");
eval(source.slice(start, end));

const malformed = `| a | b | c | d | e | f | g | h | i | j | k |
|---|---|---|---|---|---|---|---|---|---|---|
| s | e | al | fo | od | with | seals | up | good | one | x |`;

const prose = `| 五点 | 数据增强内容 | 来源文件 | 具体证据位置 |
|---|---|---|---|
| 1 | Seals food in under 5 seconds with professional suction and long evidence text that should not be squeezed into a narrow grid cell | imp_abc | search_term shows long evidence explanation with metrics, conditions, caveats, and source notes |`;

const compact = `| SKU | Sales | Orders |
|---|---:|---:|
| A1 | 120 | 3 |
| B2 | 80 | 2 |`;

const longUser = `Seals food in under 5 seconds with professional -80kPa suction—locks in flavor, prevents freezer burn, and extends freshness up to 8X longer for meat, seafood, veggies, leftovers, and sous vide meals. Charges fully in 40 minutes and seals up to 1000 bags per charge—powered by a high-density 1200mAh battery with real-time LED display and verified 98% battery longevity after 12 months. Fits any kitchen or travel bag—palm-sized cordless design saves drawer space and works flawlessly in RVs, camping trips, small apartments, and dorm rooms.`;

const cases = [
  ["malformed", malformed, out => !out.includes("<table") && out.includes("plain-block")],
  ["prose", prose, out => !out.includes("<table") && out.includes("md-records")],
  ["compact", compact, out => out.includes("<table") && !out.includes("md-records")],
  ["longUser", longUser, out => !out.includes("<table") && !out.includes("md-records") && out.includes("<p>")],
];

for (const [name, input, ok] of cases) {
  const out = fmt(input);
  const summary = {
    table: out.includes("<table"),
    records: out.includes("md-records"),
    plain: out.includes("plain-block"),
    length: out.length,
  };
  console.log(name, JSON.stringify(summary));
  if (!ok(out)) {
    console.error("failed case", name);
    console.error(out.slice(0, 1000));
    process.exit(2);
  }
}
"""
    result = subprocess.run(
        ["node", "-"],
        input=script,
        text=True,
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"frontend render check failed\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return result.stdout


def test_frontend_markdown_renderer_handles_table_edge_cases():
    output = run_frontend_render_check()
    assert "malformed" in output
    assert "prose" in output
    assert "compact" in output
    assert "longUser" in output


def test_import_history_reloads_and_excel_tab_is_primary():
    html = HTML.read_text(encoding="utf-8")
    excel_tab = '<button class="tab active" data-t="excel">Excel 导入</button>'
    sp_api_tab = '<button class="tab" data-t="amazon">SP-API 同步</button>'

    assert excel_tab in html
    assert sp_api_tab in html
    assert html.index(excel_tab) < html.index(sp_api_tab)
    assert '<div id="tab-amazon" style="display:none">' in html
    assert '<div id="tab-excel">' in html
    assert "async function loadImportHistory()" in html
    assert "fetch('/api/imports')" in html
    assert "loadCustomers();loadImportHistory();" in html


def test_chat_business_documents_have_edit_copy_export_and_versions():
    html = HTML.read_text(encoding="utf-8")

    assert "function documentBubble(content,doc,badge='')" in html
    assert "class=\"btn ghost doc-edit\"" in html
    assert "class=\"btn ghost doc-copy\"" in html
    assert "class=\"btn ghost doc-export\"" in html
    assert "class=\"btn ghost doc-history\"" in html
    assert "保存新版本" in html
    assert "createDocumentForReply(agent,text" in html


def test_product_functions_expand_when_sidebar_is_hovered():
    html = HTML.read_text(encoding="utf-8")

    assert '<div class="nav-label product-nav-label">产品功能</div>' in html
    assert ".product-nav-label,.agent-nav{max-height:0;opacity:0;overflow:hidden;pointer-events:none;flex:0 0 auto" in html
    assert ".side:hover .agent-nav,.side:focus-within .agent-nav" in html
    assert "max-height:1200px;opacity:1;pointer-events:auto" in html
