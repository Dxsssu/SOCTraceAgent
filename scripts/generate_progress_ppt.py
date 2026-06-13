from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "docs" / "report"
OUTPUT_FILE = OUTPUT_DIR / "socagent_progress_report.pptx"

EMU_PER_INCH = 914400


def emu(inches: float) -> int:
    return int(inches * EMU_PER_INCH)


def xml_text_runs(text: str, size: int = 2400, bold: bool = False, color: str = "1F2937") -> str:
    paragraphs: list[str] = []
    for raw_line in text.split("\n"):
        line = raw_line if raw_line else " "
        paragraphs.append(
            "<a:p>"
            f'<a:r><a:rPr lang="en-US" sz="{size}" b="{1 if bold else 0}" dirty="0" smtClean="0">'
            f'<a:solidFill><a:srgbClr val="{color}"/></a:solidFill>'
            "</a:rPr>"
            f"<a:t>{escape(line)}</a:t></a:r>"
            "</a:p>"
        )
    return "".join(paragraphs)


def shape_textbox(
    shape_id: int,
    name: str,
    x: int,
    y: int,
    cx: int,
    cy: int,
    text: str,
    *,
    size: int = 2400,
    bold: bool = False,
    color: str = "1F2937",
    fill: str | None = None,
    line: str | None = None,
) -> str:
    fill_xml = f'<a:solidFill><a:srgbClr val="{fill}"/></a:solidFill>' if fill else "<a:noFill/>"
    line_xml = f'<a:ln w="12700"><a:solidFill><a:srgbClr val="{line}"/></a:solidFill></a:ln>' if line else "<a:ln><a:noFill/></a:ln>"
    return (
        "<p:sp>"
        "<p:nvSpPr>"
        f'<p:cNvPr id="{shape_id}" name="{escape(name)}"/>'
        "<p:cNvSpPr txBox=\"1\"/>"
        "<p:nvPr/>"
        "</p:nvSpPr>"
        "<p:spPr>"
        f'<a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        f"{fill_xml}{line_xml}"
        "</p:spPr>"
        "<p:txBody>"
        '<a:bodyPr wrap="square" lIns="91440" tIns="45720" rIns="91440" bIns="45720"/>'
        "<a:lstStyle/>"
        f"{xml_text_runs(text, size=size, bold=bold, color=color)}"
        "</p:txBody>"
        "</p:sp>"
    )


def shape_line(shape_id: int, name: str, x1: int, y1: int, x2: int, y2: int, color: str = "94A3B8") -> str:
    return (
        "<p:cxnSp>"
        "<p:nvCxnSpPr>"
        f'<p:cNvPr id="{shape_id}" name="{escape(name)}"/>'
        "<p:cNvCxnSpPr/>"
        "<p:nvPr/>"
        "</p:nvCxnSpPr>"
        "<p:spPr>"
        f'<a:xfrm><a:off x="{x1}" y="{y1}"/><a:ext cx="{max(1, x2 - x1)}" cy="{max(1, y2 - y1)}"/></a:xfrm>'
        '<a:prstGeom prst="line"><a:avLst/></a:prstGeom>'
        f'<a:ln w="25400"><a:solidFill><a:srgbClr val="{color}"/></a:solidFill></a:ln>'
        "</p:spPr>"
        "</p:cxnSp>"
    )


def make_slide(title: str, shapes: list[str], bg_color: str = "F8FAFC") -> str:
    title_shape = shape_textbox(
        2,
        "Title",
        emu(0.6),
        emu(0.35),
        emu(12.1),
        emu(0.75),
        title,
        size=2600,
        bold=True,
        color="0F172A",
    )
    all_shapes = [
        '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>',
        '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>',
        title_shape,
        *shapes,
    ]
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
        "<p:cSld>"
        f'<p:bg><p:bgPr><a:solidFill><a:srgbClr val="{bg_color}"/></a:solidFill><a:effectLst/></p:bgPr></p:bg>'
        f"<p:spTree>{''.join(all_shapes)}</p:spTree>"
        "</p:cSld>"
        "<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>"
        "</p:sld>"
    )


def title_slide() -> str:
    shapes = [
        shape_textbox(3, "Hero", emu(0.8), emu(1.4), emu(6.0), emu(2.1), "SOCAgent\nProgress Report", size=3000, bold=True, color="0F172A"),
        shape_textbox(
            4,
            "Subtitle",
            emu(0.82),
            emu(3.65),
            emu(5.8),
            emu(1.3),
            "A three-role multi-agent prototype for security alert traceback\nCurrent focus: run the workflow end to end and keep validating and tuning on open datasets",
            size=1700,
            color="475569",
        ),
        shape_textbox(5, "Card1", emu(7.1), emu(1.35), emu(2.0), emu(1.05), "Planner\nTask Planning", size=1700, bold=True, color="FFFFFF", fill="2563EB"),
        shape_textbox(6, "Card2", emu(9.3), emu(1.35), emu(2.0), emu(1.05), "Executor\nTool Execution", size=1700, bold=True, color="FFFFFF", fill="0F766E"),
        shape_textbox(7, "Card3", emu(11.5), emu(1.35), emu(1.1), emu(1.05), "Reviewer\nReview", size=1400, bold=True, color="FFFFFF", fill="DC2626"),
        shape_textbox(8, "Flow", emu(7.1), emu(3.0), emu(5.5), emu(2.3), "Event\n↓\nTTT\n↓\nExecution\n↓\nRoundReview\n↓\nNext-round TTT", size=1700, bold=True, color="0F172A", fill="E2E8F0", line="CBD5E1"),
        shape_textbox(9, "Footer", emu(0.8), emu(6.6), emu(5.8), emu(0.35), f"Generated: {datetime.now().strftime('%Y-%m-%d')}", size=1200, color="64748B"),
    ]
    return make_slide("Advisor Report", shapes)


def progress_slide() -> str:
    left = (
        "1. Multi-role closed-loop workflow is connected\n"
        "• Planner builds the TTT (Traceback Task Tree)\n"
        "• Executor claims and executes L3 leaf nodes\n"
        "• Reviewer produces RoundReview and drives replanning\n"
        "• Core chain: Event -> TTT -> Execution -> RoundReview -> TTT"
    )
    right = (
        "2. Current validation approach\n"
        "• Shared state through SQLite for multi-process collaboration\n"
        "• Create events from the Web home page and observe progress in the war room\n"
        "• Multi-round smoke tests are available for end-to-end validation"
    )
    bottom = (
        "3. Current focus\n"
        "• Repeatedly test query quality and task decomposition on the open BOTS dataset\n"
        "• Keep tuning TTT construction, tool routing, and Splunk intent translation\n"
        "• Keep front-end status, message flow, and executions aligned with real SQLite state"
    )
    shapes = [
        shape_textbox(3, "L", emu(0.7), emu(1.25), emu(5.8), emu(2.35), left, size=1700, color="0F172A", fill="DBEAFE", line="93C5FD"),
        shape_textbox(4, "R", emu(6.85), emu(1.25), emu(5.8), emu(2.35), right, size=1700, color="0F172A", fill="DCFCE7", line="86EFAC"),
        shape_textbox(5, "B", emu(0.7), emu(4.0), emu(11.95), emu(2.2), bottom, size=1700, color="0F172A", fill="F8FAFC", line="CBD5E1"),
        shape_line(6, "divider", emu(6.45), emu(1.4), emu(6.45), emu(3.4)),
    ]
    return make_slide("1. Overall Progress", shapes)


def bots_slide() -> str:
    intro = (
        "BOTS (Boss of the SOC) is an open security-analysis dataset from Splunk,\n"
        "well suited for attack traceback, log correlation analysis, and query-strategy validation."
    )
    detail = (
        "How we use it in this project\n"
        "• botsv1 is the default dataset, with botsv2 / botsv3 reserved as well\n"
        "• Queries run through the Splunk REST API rather than front-end mock data\n"
        "• It helps validate whether the multi-round workflow generalizes across log types"
    )
    logs = (
        "Typical botsv1 log types\n"
        "• Windows / Sysmon\n"
        "• IIS / Stream / DNS / HTTP\n"
        "• Suricata / FortiGate\n"
        "• Registry and host-behavior logs"
    )
    value = (
        "Why it fits the current stage\n"
        "• Public data makes experiments easy to reproduce\n"
        "• Includes both network traffic and host logs\n"
        "• Supports full validation from alert to evidence chain"
    )
    shapes = [
        shape_textbox(3, "Intro", emu(0.7), emu(1.2), emu(12.0), emu(1.0), intro, size=1800, color="0F172A", fill="FEF3C7", line="FCD34D"),
        shape_textbox(4, "Detail", emu(0.7), emu(2.45), emu(5.8), emu(2.6), detail, size=1650, color="0F172A", fill="EFF6FF", line="93C5FD"),
        shape_textbox(5, "Logs", emu(6.9), emu(2.45), emu(2.75), emu(2.6), logs, size=1600, color="0F172A", fill="ECFDF5", line="86EFAC"),
        shape_textbox(6, "Value", emu(9.95), emu(2.45), emu(2.75), emu(2.6), value, size=1600, color="0F172A", fill="FDF2F8", line="F9A8D4"),
    ]
    return make_slide("2. BOTS Dataset", shapes)


def stack_slide() -> str:
    backend = (
        "Backend\n"
        "• Python 3.13\n"
        "• Flask + Flask-SocketIO\n"
        "• SQLite as the shared state hub\n"
        "• OpenAI-compatible LLM integration\n"
        "• Splunk / VirusTotal / IPInfo tool integration"
    )
    frontend = (
        "Frontend\n"
        "• Jinja2 template pages\n"
        "• Bootstrap 5\n"
        "• Vanilla JavaScript\n"
        "• Socket.IO real-time message updates\n"
        "• Home page + War Room structure"
    )
    arch = (
        "Runtime structure\n"
        "Web layer\n"
        "↓\n"
        "Planner / Executor / Reviewer\n"
        "↓\n"
        "SQLite + TTT + Execution + Review"
    )
    shapes = [
        shape_textbox(3, "Backend", emu(0.7), emu(1.35), emu(4.0), emu(3.5), backend, size=1700, color="FFFFFF", fill="1D4ED8"),
        shape_textbox(4, "Frontend", emu(4.95), emu(1.35), emu(4.0), emu(3.5), frontend, size=1700, color="FFFFFF", fill="0F766E"),
        shape_textbox(5, "Arch", emu(9.2), emu(1.35), emu(3.45), emu(3.5), arch, size=1650, bold=True, color="0F172A", fill="E2E8F0", line="94A3B8"),
        shape_textbox(6, "Bottom", emu(0.7), emu(5.2), emu(11.95), emu(1.1), "The current system is not a static demo page. It is a runnable prototype where the frontend and backend consume real runtime state together.", size=1600, color="334155", fill="F8FAFC", line="CBD5E1"),
    ]
    return make_slide("3. Frontend and Backend Stack", shapes)


def ui_slide() -> str:
    home = (
        "Home page\n"
        "• Create security events\n"
        "• View the event list\n"
        "• Enter a single-event war room"
    )
    war = (
        "War Room\n"
        "• Event details and status\n"
        "• Three-role message stream\n"
        "• TTT hierarchy\n"
        "• Execution records and round reviews\n"
        "• Socket.IO real-time updates"
    )
    next_steps = (
        "Next steps\n"
        "• Keep testing different alert types on BOTS\n"
        "• Improve TTT quality and Splunk query generation\n"
        "• Expand callable security tools\n"
        "• Add more standardized tests and stronger UI observability"
    )
    shapes = [
        shape_textbox(3, "HomeFrame", emu(0.8), emu(1.45), emu(4.0), emu(3.8), "", fill="FFFFFF", line="94A3B8"),
        shape_textbox(4, "HomeNav", emu(1.0), emu(1.7), emu(3.6), emu(0.45), "SOCAgent Home", size=1500, bold=True, color="FFFFFF", fill="2563EB"),
        shape_textbox(5, "HomeLeft", emu(1.05), emu(2.35), emu(1.4), emu(2.1), "Event Form", size=1700, bold=True, color="0F172A", fill="DBEAFE", line="93C5FD"),
        shape_textbox(6, "HomeRight", emu(2.65), emu(2.35), emu(1.9), emu(2.1), "Event List\n\nView created investigations\nby status and time", size=1450, color="0F172A", fill="F8FAFC", line="CBD5E1"),
        shape_textbox(7, "HomeNote", emu(0.95), emu(5.45), emu(3.7), emu(0.7), home, size=1350, color="334155"),
        shape_textbox(8, "WarFrame", emu(5.15), emu(1.45), emu(4.2), emu(3.8), "", fill="0F172A", line="38BDF8"),
        shape_textbox(9, "WarHeader", emu(5.35), emu(1.7), emu(3.8), emu(0.45), "SOCAgent War Room", size=1500, bold=True, color="FFFFFF", fill="0EA5E9"),
        shape_textbox(10, "WarMain", emu(5.4), emu(2.35), emu(2.55), emu(2.2), "Message Stream\n\nPlanner / Executor /\nReviewer collaboration", size=1450, color="E2E8F0", fill="111827", line="334155"),
        shape_textbox(11, "WarSide", emu(8.15), emu(2.35), emu(1.0), emu(2.2), "Status\n\nRound\nLeaves\nExecs\nRoles", size=1350, color="E2E8F0", fill="1E293B", line="334155"),
        shape_textbox(12, "WarNote", emu(5.3), emu(5.45), emu(3.9), emu(0.8), war, size=1300, color="334155"),
        shape_textbox(13, "NextSteps", emu(9.7), emu(1.45), emu(2.7), emu(4.7), next_steps, size=1450, color="0F172A", fill="FEF3C7", line="FCD34D"),
    ]
    return make_slide("4. UI Preview and Next Steps", shapes)


def rels_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
        "</Relationships>"
    )


def content_types_xml(slide_count: int) -> str:
    overrides = "".join(
        f'<Override PartName="/ppt/slides/slide{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        for i in range(1, slide_count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
        '<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>'
        '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
        '<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        '<Override PartName="/ppt/presProps.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presProps+xml"/>'
        '<Override PartName="/ppt/viewProps.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.viewProps+xml"/>'
        '<Override PartName="/ppt/tableStyles.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.tableStyles+xml"/>'
        f"{overrides}"
        "</Types>"
    )


def app_xml(slide_count: int) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        "<Application>Microsoft Office PowerPoint</Application>"
        "<PresentationFormat>Custom</PresentationFormat>"
        f"<Slides>{slide_count}</Slides>"
        "<Notes>0</Notes>"
        "<HiddenSlides>0</HiddenSlides>"
        "<MMClips>0</MMClips>"
        "<ScaleCrop>false</ScaleCrop>"
        "<HeadingPairs><vt:vector size=\"2\" baseType=\"variant\"><vt:variant><vt:lpstr>Slides</vt:lpstr></vt:variant><vt:variant><vt:i4>"
        f"{slide_count}"
        "</vt:i4></vt:variant></vt:vector></HeadingPairs>"
        f"<TitlesOfParts><vt:vector size=\"{slide_count}\" baseType=\"lpstr\">"
        + "".join(f"<vt:lpstr>Slide {i}</vt:lpstr>" for i in range(1, slide_count + 1))
        + "</vt:vector></TitlesOfParts>"
        "<Company></Company>"
        "<LinksUpToDate>false</LinksUpToDate>"
        "<SharedDoc>false</SharedDoc>"
        "<HyperlinksChanged>false</HyperlinksChanged>"
        "<AppVersion>16.0000</AppVersion>"
        "</Properties>"
    )


def core_xml() -> str:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:dcmitype="http://purl.org/dc/dcmitype/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        "<dc:title>SOCAgent Progress Report</dc:title>"
        "<dc:creator>Codex</dc:creator>"
        "<cp:lastModifiedBy>Codex</cp:lastModifiedBy>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>'
        "</cp:coreProperties>"
    )


def presentation_xml(slide_count: int) -> str:
    slide_ids = "".join(f'<p:sldId id="{255 + i}" r:id="rId{i + 1}"/>' for i in range(1, slide_count + 1))
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
        '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
        f"<p:sldIdLst>{slide_ids}</p:sldIdLst>"
        "<p:sldSz cx=\"12188952\" cy=\"6858000\"/>"
        "<p:notesSz cx=\"6858000\" cy=\"9144000\"/>"
        "<p:defaultTextStyle/>"
        "</p:presentation>"
    )


def presentation_rels_xml(slide_count: int) -> str:
    slide_rels = "".join(
        f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide{i}.xml"/>'
        for i in range(1, slide_count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>'
        f"{slide_rels}"
        "</Relationships>"
    )


def slide_rels_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
        "</Relationships>"
    )


def slide_master_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:sldMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
        "<p:cSld><p:bg><p:bgRef idx=\"1001\"><a:schemeClr val=\"bg1\"/></p:bgRef></p:bg><p:spTree>"
        '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
        '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
        "</p:spTree></p:cSld>"
        '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>'
        '<p:sldLayoutIdLst><p:sldLayoutId id="1" r:id="rId1"/></p:sldLayoutIdLst>'
        "<p:txStyles/>"
        "</p:sldMaster>"
    )


def slide_master_rels_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/>'
        "</Relationships>"
    )


def slide_layout_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:sldLayout xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" type="blank" preserve="1">'
        "<p:cSld name=\"Blank\"><p:spTree>"
        '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
        '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
        "</p:spTree></p:cSld>"
        '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>'
        "</p:sldLayout>"
    )


def slide_layout_rels_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/>'
        "</Relationships>"
    )


def theme_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Office Theme">'
        "<a:themeElements>"
        "<a:clrScheme name=\"Office\">"
        "<a:dk1><a:srgbClr val=\"000000\"/></a:dk1>"
        "<a:lt1><a:srgbClr val=\"FFFFFF\"/></a:lt1>"
        "<a:dk2><a:srgbClr val=\"1F2937\"/></a:dk2>"
        "<a:lt2><a:srgbClr val=\"F8FAFC\"/></a:lt2>"
        "<a:accent1><a:srgbClr val=\"2563EB\"/></a:accent1>"
        "<a:accent2><a:srgbClr val=\"0F766E\"/></a:accent2>"
        "<a:accent3><a:srgbClr val=\"DC2626\"/></a:accent3>"
        "<a:accent4><a:srgbClr val=\"F59E0B\"/></a:accent4>"
        "<a:accent5><a:srgbClr val=\"7C3AED\"/></a:accent5>"
        "<a:accent6><a:srgbClr val=\"0EA5E9\"/></a:accent6>"
        "<a:hlink><a:srgbClr val=\"2563EB\"/></a:hlink>"
        "<a:folHlink><a:srgbClr val=\"7C3AED\"/></a:folHlink>"
        "</a:clrScheme>"
        "<a:fontScheme name=\"Office\">"
        "<a:majorFont><a:latin typeface=\"Aptos Display\"/><a:ea typeface=\"Arial\"/><a:cs typeface=\"Arial\"/></a:majorFont>"
        "<a:minorFont><a:latin typeface=\"Aptos\"/><a:ea typeface=\"Arial\"/><a:cs typeface=\"Arial\"/></a:minorFont>"
        "</a:fontScheme>"
        '<a:fmtScheme name="Office"><a:fillStyleLst/><a:lnStyleLst/><a:effectStyleLst/><a:bgFillStyleLst/></a:fmtScheme>'
        "</a:themeElements>"
        "</a:theme>"
    )


def minimal_xml(path_tag: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><{path_tag} xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>'


def build_pptx() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    slides = [
        title_slide(),
        progress_slide(),
        bots_slide(),
        stack_slide(),
        ui_slide(),
    ]
    with ZipFile(OUTPUT_FILE, "w", compression=ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types_xml(len(slides)))
        zf.writestr("_rels/.rels", rels_xml())
        zf.writestr("docProps/app.xml", app_xml(len(slides)))
        zf.writestr("docProps/core.xml", core_xml())
        zf.writestr("ppt/presentation.xml", presentation_xml(len(slides)))
        zf.writestr("ppt/_rels/presentation.xml.rels", presentation_rels_xml(len(slides)))
        zf.writestr("ppt/slideMasters/slideMaster1.xml", slide_master_xml())
        zf.writestr("ppt/slideMasters/_rels/slideMaster1.xml.rels", slide_master_rels_xml())
        zf.writestr("ppt/slideLayouts/slideLayout1.xml", slide_layout_xml())
        zf.writestr("ppt/slideLayouts/_rels/slideLayout1.xml.rels", slide_layout_rels_xml())
        zf.writestr("ppt/theme/theme1.xml", theme_xml())
        zf.writestr("ppt/presProps.xml", minimal_xml("p:presentationPr"))
        zf.writestr("ppt/viewProps.xml", minimal_xml("p:viewPr"))
        zf.writestr("ppt/tableStyles.xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><a:tblStyleLst xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" def=""/>')
        for idx, slide in enumerate(slides, start=1):
            zf.writestr(f"ppt/slides/slide{idx}.xml", slide)
            zf.writestr(f"ppt/slides/_rels/slide{idx}.xml.rels", slide_rels_xml())


if __name__ == "__main__":
    build_pptx()
    print(OUTPUT_FILE)
