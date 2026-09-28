import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from wsdc_catalog.api import serve
from wsdc_catalog.config import Config
import json as _json
from tests.helpers import day1_fetch, fx

with open(fx("registry_29012.json")) as _f:
    _TONI = _json.load(_f)


def _sheet_fetch(url):
    pages = {"https://eepro.com/results/2026/": "eepro_index_2026.html",
             "https://eepro.com/results/swingtime2026/jjprelims.html": "eepro_prelims.html",
             "https://eepro.com/results/swingtime2026/jjfinals.html": "eepro_finals.html"}
    if url not in pages:
        raise RuntimeError("404")
    with open(fx(pages[url]), encoding="utf-8") as f:
        return f.read()


class TestAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cfg = Config(db_path=str(Path(cls.tmp.name) / "c.sqlite3"),
                     snapshot_dir=str(Path(cls.tmp.name) / "snaps"),
                     catalog_api_key="read-key", admin_api_key="admin-key")
        cls.server = serve(cfg, "127.0.0.1", 0, fetcher=day1_fetch(),
                           registry_fetch=lambda q: _TONI if q.strip() in ("29012", "toni watt", "Toni Watt") else [],
                           sheet_fetch=_sheet_fetch)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        status, cls.sync = cls.req("POST", "/admin/sync-events", key="admin-key", body={})
        assert status == 200, cls.sync

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.tmp.cleanup()

    @classmethod
    def req(cls, method, path, key="read-key", body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(cls.base + path, data=data, method=method)
        if key:
            r.add_header("Authorization", f"Bearer {key}")
        if data:
            r.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_sync_snapshot_saved(self):
        self.assertTrue(Path(self.sync["snapshot_path"]).exists())

    def test_filters(self):
        _, all_ = self.req("GET", "/events?active=true&limit=500")
        self.assertEqual(all_["total"], 35)
        _, y = self.req("GET", "/events?year=2026&country=US")
        self.assertTrue(y["items"] and all(e["year"] == 2026 and e["country"] == "USA" for e in y["items"]))
        _, s = self.req("GET", "/events?search=boogie")
        self.assertEqual([e["name"] for e in s["items"]], ["Boogie By The Bay"])
        _, a = self.req("GET", "/events?search=tap")
        self.assertIn("The After Party aka TAP", [e["name"] for e in a["items"]])
        _, t = self.req("GET", "/events?event_status=trial")
        self.assertTrue(all(e["event_status"] == "trial" for e in t["items"]))

    def test_pagination(self):
        _, p = self.req("GET", "/events?limit=10&offset=10")
        self.assertEqual((len(p["items"]), p["offset"]), (10, 10))

    def test_bad_params(self):
        self.assertEqual(self.req("GET", "/events?event_status=gold")[0], 400)
        self.assertEqual(self.req("GET", "/events?year=abc")[0], 400)

    def test_get_event_and_series(self):
        _, lst = self.req("GET", "/events?search=madjam")
        eid = lst["items"][0]["id"]
        status, ev = self.req("GET", f"/events/{eid}")
        self.assertEqual((status, ev["canonical_name"]), (200, "MADjam"))
        self.assertIn("raw_payload", ev)
        status, series = self.req("GET", f"/series/{ev['series_id']}")
        self.assertEqual(status, 200)
        self.assertEqual(self.req("GET", "/events/evt_missing")[0], 404)

    def test_auth(self):
        self.assertEqual(self.req("GET", "/events", key=None)[0], 401)
        self.assertEqual(self.req("GET", "/events", key="wrong")[0], 401)
        self.assertEqual(self.req("POST", "/admin/sync-events", key="read-key", body={})[0], 401)
        self.assertEqual(self.req("GET", "/health", key=None)[0], 200)

    def test_resolve_and_alias(self):
        _, res = self.req("POST", "/events/resolve", body={"name": "Boogie By The Bay", "year": 2026})
        self.assertEqual(res["status"], "resolved")
        sid = res["event"]["series_id"]
        status, out = self.req("POST", f"/admin/series/{sid}/aliases", key="admin-key",
                               body={"alias": "BBB", "source": "scoring_dance"})
        self.assertEqual(status, 201)
        _, runs = self.req("GET", "/admin/sync-runs", key="admin-key")
        self.assertEqual(runs["items"][0]["status"], "success")

    def test_competitor_endpoints(self):
        status, out = self.req("GET", "/competitors/29012")
        self.assertEqual((status, out["first_name"], len(out["placements"])), (200, "Toni", 1))
        self.assertTrue(out["placements"][0]["catalog_event_id"].startswith("evt_"))
        status, s = self.req("GET", "/competitors/search?q=Toni%20Watt")
        self.assertEqual((status, s["items"][0]["wsdc_id"]), (200, 29012))
        self.assertEqual(self.req("GET", "/competitors/424242")[0], 404)
        self.assertEqual(self.req("GET", "/competitors/search?q=a")[0], 400)
        self.assertEqual(self.req("GET", "/competitors/29012", key=None)[0], 401)

    def test_scoresheet_endpoints(self):
        self.assertEqual(self.req("POST", "/admin/scoresheets/discover", key="read-key", body={"year": 2026})[0], 401)
        status, out = self.req("POST", "/admin/scoresheets/discover", key="admin-key", body={"year": 2026})
        self.assertEqual((status, out["runs"][0]["events"]), (200, 3))
        _, evs = self.req("GET", "/events?year=2026&results_available=true")
        st = [e for e in evs["items"] if e["name"] == "SwingTime"][0]
        status, res = self.req("POST", "/scoresheets/lookup", body={"name": "Emily Madden", "event_ids": [st["id"]]})
        self.assertEqual((status, res["events"][0]["status"]), (200, "found"))
        self.assertEqual(res["events"][0]["divisions"][-1]["final_place"], 1)
        self.assertEqual(self.req("POST", "/scoresheets/lookup", body={"name": "Emily Madden"})[0], 400)
        self.assertEqual(self.req("GET", "/scoresheets/coverage")[0], 200)
