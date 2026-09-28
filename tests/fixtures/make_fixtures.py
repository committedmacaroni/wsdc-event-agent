"""Build HTML fixtures shaped like https://worldsdc.com/events/.

Rows are copied from the live WSDC event list as rendered on 2026-09-24 and chosen to
cover edge cases. The HTML markup is RECONSTRUCTED from the rendered table (link, status
label, flag link); verify the parser against a live snapshot before trusting it.

Unconfirmed rows are an assumption for testing: rows with no Registry/Trial label are
treated as the ones hidden by ?hideunconfirmed=1.

Day 2 (2026-09-30) is synthetic: it simulates realistic changes.
"""
from html import escape
from pathlib import Path

HERE = Path(__file__).parent

# (date, name, website, status_label, location, flag)
DAY1 = [
    ("Sep 23 - 27, 2026", "The Aloha Open", "http://www.alohaopenwcs.com", "Registry Event", "Wailea, Hawaii/Maui, USA", "USA"),
    ("Sep 24 - 28, 2026", "Mooseland Swing 2026", "https://mooselandswing.com/", "Registry Event", "Östersund, Jämtland, Sweden", "SWE"),
    ("Sep 24 - 28, 2026", "Meet Me In St Louis", "http://Www.mmisl.com", "Registry Event", "St. Louis, Mo, USA", "USA"),
    ("Sep 25 - 27, 2026", "Philly Swing Classic", "https://phillyswing.com/", "Registry Event", "WILMINGTON, DEL, Delaware, United States", "USA"),
    ("Oct 1 - 5, 2026", "RiverSwingNights", "http://www.riverswingnights.com", "Trial Event", "Dresden, Germany", "DEU"),
    ("Oct 1 - 5, 2026", "Westie Harbor", "http://www.westieharbor.com", "Trial Event", "Gothenburg, n/a, Sweden", "SWE"),
    ("Oct 8 - 12, 2026", "Boogie By The Bay", "https://boogiebythebay.com", "Registry Event", "San Francisco, CA, USA", "USA"),
    ("Oct 15 - 17, 2026", "WCS Festival", "http://www.wcsfestival.com", "Registry Event", "Boston Club, NRW, Germany", "DEU"),
    ("Oct 16 - 18, 2026", "WCS Festival", "http://www.wcsfestival.com", "Registry Event", "Duesseldorf, NRW, Germany", "DEU"),
    ("Oct 22 - 24, 2026", "Swingside Invitational", "http://www.swingside-invitational.com", "Registry Event", "Liège, , Belgium", "BEL"),
    ("Oct 22 - 25, 2026", "SASS- Spooky Albany Swing Spectacular", "http://www.spookyalbanyswing.com", "Registry Event", "Albany, NY, Albany", "USA"),
    ("Oct 23 - 25, 2026", "Swingside Invitational", "https://www.swingside-invitational.com", "Registry Event", "Liège, Liège, Belgique", "BEL"),
    ("Oct 28 - Nov 2, 2026", 'Scandinavian Open WCS "SNOW"', "http://www.snowcs.se", "Registry Event", "Stockholm, Sweden, Sverige", "SWE"),
    ("Oct 29 - Nov 2, 2026", "Warsaw Halloween Swing", "http://www.warsawhalloweenswing.com", "", "Warsaw, Poland", "POL"),
    ("Nov 6 - 8, 2026", "Moscow Westie Fest Gala Edition", "http://westiefest.org/", "Registry Event", "Moscow, Russia", "transparent"),
    ("Nov 19 - 22, 2026", "DC Swing eXperience (DCSX)", "http://www.dcswingexperience.com", "", "Washington, DC., VA, USA", "USA"),
    ("Nov 25 - 29, 2026", "The Open Swing Dance Championships", "http://www.theopenswing.com", "", "Los Angeles, CA, United States", "USA"),
    ("Dec 3 - 7, 2026", "The After Party aka TAP", "http://www.tapwcs.com", "Registry Event", "Orange County, CA, USA", "USA"),
    ("Dec 11 - 13, 2026", "Soul Flow - West Coast Swing Festival (Hiatus -- 2026)", "https://www.globalgrandprixwcs.com/", "Registry Event", "Toulouse-Blagnac, Occitanie, France", "FRA"),
    ("Dec 18 - 20, 2026", "Swiss Open WCS", "http://www.swissopenwcs.ch", "Trial Event", "Geneva, Switzerland", "CHE"),
    ("Dec 28 2026 - Jan 3 2027", "UCWDC Country Dance World Championships", "http://www.ucwdcworlds.com", "Registry Event", "Dallas, TX, USA", "USA"),
    ("Dec 30 2026 - Jan 3 2027", "Floorplay Swing Vacation", "http://www.floorplayswingvacation.com", "", "Orlando, Florida, United States", "USA"),
    ("Feb 4 - 7, 2027", "Waterloo Ontario Open West Coast Swing Championships", "http://www.waterlooopenwcs.com", "Registry Event", "Waterloo, Ontario, Canada", "USA"),
    ("Feb 4 - 7, 2027", "Waterloo Ontario Open West Coast Swing Championships", "http://www.waterlooopenwcs.com", "Registry Event", "Waterloo, Ontario, Canada", "CAN"),
    ("Feb 25 - Mar 1, 2027", "Paris Swing Classic", "http://Https://parisswingclassic.com", "Registry Event", "Paris, France/Paris, France", "FRA"),
    ("Feb 26 - 28, 2027", "New York Flow Festival", "https://www.flowfest.nyc", "Registry Event", "New York City, NY, US", ""),
    ("Mar 4 - 8, 2027", "MADjam (Mid Atlantic Dance Jam)", "http://www.atlanticdancejam.com", "Registry Event", "Washington, DC, VA, USA", "USA"),
    ("Apr 22 - 25, 2027", "HONEY FEST", "https://t.me/honeyfest", "Registry Event", "Ufa, Russia", "transparent"),
    ("Apr 29 - May 2, 2027", "Swingin' Into Spring", "http://www.sis.djkenm.com", "Registry Event", "Hartford, CT", "USA"),
    ("Aug 5 - 8, 2027", "Swingtacular: The Galactic Open", "http://www.Swingtacular.art", "Registry Event", "San Francisco, CA, United States", "USA"),
    ("Aug 12 - 16, 2027", "Grand Party Sofia (GPS)", "https://wcs-gps.com/", "Registry Event", "Sofia, Sofia, Bulgaria", "BGR"),
    ("Oct 1 - 4, 2026", "Atlanta Swing Classic", "https://www.atlantaswingclassic.com/", "Registry Event", "Atlanta, GA, USA", "USA"),
    ("Oct 7 - 10, 2027", "Atlanta Swing Classic", "https://www.atlantaswingclassic.com/", "Registry Event", "Atlanta, Georgia, USA", "USA"),
    ("Sep 28 - Oct 1, 2028", "Atlanta Swing Classic", "https://www.atlantaswingclassic.com/", "Registry Event", "Atlanta, GA, USA", "USA"),
    ("Feb 9 - 14, 2027", "Euro Dance Festival", "https://www.euro-dance-festival.com", "Registry Event", "Rust, Baden-Württemberg, Germany", "DEU"),
    ("Feb 29 - Mar 5, 2028", "Euro Dance Festival", "https://www.euro-dance-festival.com", "Registry Event", "Rust, Baden-Württemberg, Germany", "DEU"),
]


def day2_rows():
    rows = []
    for r in DAY1:
        date, name, url, status, loc, flag = r
        if name == "The Aloha Open":          # ended Sep 27 -> drops off -> past
            continue
        if name == "HONEY FEST":              # future event vanishes -> missing_from_source
            continue
        if name == "Swiss Open WCS":          # trial -> registry
            status = "Registry Event"
        if name == "Boogie By The Bay":       # date change, caught by fuzzy match
            date = "Oct 9 - 12, 2026"
        if name == "Warsaw Halloween Swing":  # director submitted the update form
            status = "Registry Event"
        rows.append((date, name, url, status, loc, flag))
    rows.append(("Jan 15 - 17, 2027", "Fixture Swing Weekend", "https://example.com/fsw",
                 "Trial Event", "Sacramento, CA, USA", "USA"))
    return rows


def unconfirmed(row) -> bool:
    return row[3] == ""


def render(rows) -> str:
    trs = []
    for date, name, url, status, loc, flag in rows:
        label = f"&nbsp;&nbsp;<span class=\"event-type\">{escape(status)}</span>" if status else ""
        trs.append(
            "<tr>"
            f"<td>{escape(date)}</td>"
            f"<td><a href=\"{escape(url)}\" target=\"_blank\">{escape(name)}</a>{label}</td>"
            f"<td>{escape(loc)}</td>"
            f"<td><a href=\"https://worldsdc.com/events/?country={escape(flag)}\">"
            f"<img src=\"https://library.goo1.de/flags/{escape(flag)}.png\"></a></td>"
            "</tr>")
    return ("<!doctype html><html><head><meta charset=\"utf-8\"><title>Event List - World Swing Dance Council"
            "</title></head><body><h1>Event List</h1><p><strong>NOTE:</strong> For an event listed as "
            "&ldquo;unconfirmed,&rdquo; the Event Director has not yet submitted this update form.</p>"
            "<table><thead><tr><th>Date</th><th>Event Name</th><th>Event Location</th><th>Country</th>"
            "</tr></thead><tbody>\n" + "\n".join(trs) + "\n</tbody></table></body></html>\n")


def main():
    d2 = day2_rows()
    files = {
        "wsdc_day1.html": render(DAY1),
        "wsdc_day1_confirmed.html": render([r for r in DAY1 if not unconfirmed(r)]),
        "wsdc_day2.html": render(d2),
        "wsdc_day2_confirmed.html": render([r for r in d2 if not unconfirmed(r)]),
        # Simulated layout change: the table is gone.
        "wsdc_broken.html": "<html><body><div class='events-grid'>New layout</div></body></html>\n",
        # Simulated partial parse: only 5 rows survive.
        "wsdc_partial.html": render(DAY1[:5]),
    }
    for name, html in files.items():
        (HERE / name).write_text(html, encoding="utf-8")
    print(f"day1={len(DAY1)} rows, day2={len(d2)} rows")


if __name__ == "__main__":
    main()
