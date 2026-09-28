"""Event Express Pro fixtures. Row data copied from the real SwingTime 2026 sheets
(https://eepro.com/results/swingtime2026/); the HTML markup is RECONSTRUCTED as tables,
plus one list-based variant to exercise the whitespace fallback. Verify against live pages."""
from html import escape
from pathlib import Path

HERE = Path(__file__).parent
TIE = "When marks are tied, lowest sum used as tiebreaker, Y=10, ALT1=4.5, ALT2=4.3, ALT3=4.2, N=0"

PRELIMS = [
    ("Jack & Jill Follower Novice Prelims - 66 competed",
     ["Bella Viramontes", "Mackenzie Goodmanson", "Susan Kirklin", "Talía Colón"], [
        "1|Rose Landay|Y|Y|Y|Y|40|4-0-0|40|X|",
        "18|Rachel Hughes|Y|Y|N|Y|10|3-0-1|30|X|ALT1",
        "27|Octavia Betz|Y|A1|Y|N|159|2-1-1|24.5|X|ALT2",
        "|Tanya Davis|Y|Y|A3|N|126|2-1-1|24.2||",
        "|Mary Eiberger|N|N|N|N|60|0-0-4|0||"]),
    ("Jack & Jill Follower Novice Semis - 25 competed",
     ["Bella Viramontes", "Mackenzie Goodmanson", "Susan Kirklin", "Talía Colón"], [
        "10|Rose Landay|N|Y|Y|N|40|2-0-2|20|X|ALT1",
        "1|Emily Madden|Y|Y|Y|Y|172|4-0-0|40|X|"]),
    ("Jack & Jill Leader Novice Prelims - 48 competed",
     ["Aidan Keith-hynes", "Conor Mcclure", "Gary Mcintyre", "Glenn Ball"], [
        "1|Josh Schroeder|Y|Y|Y|Y|321|4-0-0|40|X|",
        "|Martin Saunders|N|N|N|Y|189|1-0-3|10||"]),
    ("All-In Prelims - 62 competed",
     ["Yvonne Antonacci", "Bella Viramontes", "Jim Tigges", "Sam Vaden", "Thomas Carter"], [
        "1|Andrew Opyrchal and Rose Landay|Y|Y|Y|Y|Y|28|5-0-0|50|X|",
        "|Martin Saunders and Tanya Davis|N|N|N|N|Y|189|1-0-4|10||"]),
]
FINALS = [
    ("Division: Jack & Jill Novice Finals",
     ["Aidan Keith-hynes", "Andrew Opyrchal", "Sam Vaden", "Sebastian Quinones", "Talía Colón"], [
        "1|Josh Schroeder and Emily Madden|1|1|4|5|2|321/172|1-1-2-4-5",
        "4|Cameron Cordova and Abigail Sewall|6|8|6|1|5|236/44|1-5-6-6-8"]),
    ("Division: All-In Finals",
     ["Bella Viramontes", "Jim Tigges", "Lisa Picard", "Sam Vaden", "Thomas Carter"], [
        "2|Andrew Opyrchal and Rose Landay|2|1|7|5|2|28/98|1-2-2-5-7"]),
]


def table_sheet(sections, kind):
    """Mirrors the live eepro markup (confirmed 2026-09-28): the title and notes share one
    colspan cell separated by <br>, cells are separated by newlines and tabs in the source,
    and the bib sits inside <div class="contentblack">."""
    head = ("Count", "Competitor") if kind == "prelim" else ("Place", "Competitor")
    tail = ("BIB", "Counts (Y-A-N)", "Sum", "Promote", "Alt") if kind == "prelim" else ("BIB", "Marks Sorted")
    parts = ['<html><head><meta http-equiv="Content-Type" content="text/html; charset=UTF-8">\n'
             "    <title>Event Express Pro Floor Interface</title>\n</head>\n<body>\n\n<p>&nbsp;</p>\n"
             "<h2><center>SwingTime Denver - 2026</center></h2>\n"
             '<table border="1" cellspacing="1" cellpadding="10" align="center" width="100%">\n\n</table><p></p>']
    for title, judges, rows in sections:
        ncols = len(head) + len(judges) + len(tail)
        note = f"<br>{TIE}<br>All ties broken by head judge." if kind == "prelim" else ""
        parts.append(f'<table border="1" cellspacing="1" cellpadding="2" align="center" width="100%"><tbody>'
                     f'<tr bgcolor="#ffae5e"><td colspan="{ncols}">{escape(title)}{note}</td></tr><tr>\n')
        hdr = [f"\t<td><em><strong>{escape(head[0])}</strong></em></td>\n",
               f'\t<td width="400"><em><strong>{escape(head[1])}</strong></em></td>']
        hdr += [f"<td><em><strong>{escape(j)}</strong></em></td>" for j in judges]
        hdr += [f"<td><em><strong>{escape(tail[0])}</strong></em></td>\n"]
        hdr += [f"\t<td><em><strong>{escape(t)}</strong></em></td>\n" for t in tail[1:]]
        parts.append("".join(hdr) + "</tr>")
        nj = len(judges)
        for r in rows:
            c = r.split("|")
            cells = [f"<tr>\n\t<td>{escape(c[0])}</td>\n\t<td>{escape(c[1])}</td>"]
            cells += [f"<td>{escape(x)}</td>" for x in c[2:2 + nj]]
            cells += [f'<td><div class="contentblack">{escape(c[2 + nj])}</div></td>\n']
            cells += [f"\t<td>{escape(x)}</td>\n" for x in c[3 + nj:]]
            parts.append("".join(cells) + "</tr>")
        parts.append("</tbody></table><p></p>")
    parts.append("<p>INSTANT SCORING provided by Paul Stoddard</p></body></html>")
    return "".join(parts)


def list_sheet(sections):
    """Same content with no tables: each row is a list item of space-separated text."""
    parts = ["<html><body><h2>SwingTime Denver - 2026</h2><ul>"]
    for title, judges, rows in sections:
        parts.append(f"<li>{escape(title)}</li><li>{TIE}</li>")
        parts.append("<li>Count Competitor " + " ".join(judges) + " BIB Counts (Y-A-N) Sum Promote Alt</li>")
        for r in rows:
            parts.append("<li>" + escape(" ".join(c for c in r.split("|") if c)) + "</li>")
    parts.append("</ul></body></html>")
    return "\n".join(parts)


INDEX = """<html><body><h1>Judge Marks and Results Menu</h1><h1>2026 Results</h1><ul>
<li><input type="checkbox"> September 10-13, 2026 - SwingTime<ul>
 <li><a href="https://eepro.com/results/swingtime2026/jjprelims.html">Jack and Jill Prelims Rounds</a></li>
 <li><a href="https://eepro.com/results/swingtime2026/jjfinals.html">Jack and Jill Finals</a></li>
 <li><a href="/results/swingtime2026/routines.pdf">Routines</a></li></ul></li>
<li><input type="checkbox"> July 30-Aug 2, 2026 - Arizona Dance Classic<ul>
 <li><a href="https://eepro.com/results/adc2026/ctstcountryswingprelims.html">J&amp;J CTST and Country Swing Prelims</a></li></ul></li>
<li><input type="checkbox"> Feb 27-Mar 1, 2026 - New York Flow Festival<ul>
 <li><a href="https://eepro.com/results/flowfest2026/jjprelims.html">Jack and Jill Prelims</a></li></ul></li>
</ul></body></html>"""

if __name__ == "__main__":
    (HERE / "eepro_prelims.html").write_text(table_sheet(PRELIMS, "prelim"), encoding="utf-8")
    (HERE / "eepro_finals.html").write_text(table_sheet(FINALS, "final"), encoding="utf-8")
    (HERE / "eepro_prelims_list.html").write_text(list_sheet(PRELIMS), encoding="utf-8")
    (HERE / "eepro_index_2026.html").write_text(INDEX, encoding="utf-8")
    print("ok")
