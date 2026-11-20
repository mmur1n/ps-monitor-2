import unittest
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory
from monitor import PRODUCTS, classify, once

def fixture(p, status, valid=True):
    if p["kind"].startswith("disc"):
        spec="Diese Konsole hat ein Laufwerk. 1-TB-SSD. " + ("2 DualSense." if p["kind"]=="disc-two" else "DualSense.")
    else:
        spec="Konsole ohne Laufwerk. 825-GB-SSD. 2 DualSense."
    if not valid: spec="Wrong product."
    return ("<html><body><p>"+"Product details. "*50+"</p><p>"+spec+"</p><h1>"+
            p["title"].replace("PlayStation5","PlayStation®5")+"</h1><div>"+status+
            "</div><p>"+"Legal documentation. "*40+"</p></body></html>")

class Tests(unittest.TestCase):
    def test_stock(self):
        for p in PRODUCTS:
            self.assertEqual(classify(fixture(p,"Nicht zum Kauf erhältlich"),p)["status"],"OUT_OF_STOCK")
            self.assertEqual(classify(fixture(p,"In den Warenkorb legen"),p)["status"],"IN_STOCK")
    def test_unknown(self):
        self.assertEqual(classify(fixture(PRODUCTS[0],"Mehr erfahren"),PRODUCTS[0])["status"],"UNKNOWN")
    def test_wrong_variant(self):
        self.assertEqual(classify(fixture(PRODUCTS[1],"In den Warenkorb",False),PRODUCTS[1])["status"],"UNKNOWN")
    def test_conflict_negative_wins(self):
        self.assertEqual(classify(fixture(PRODUCTS[0],"In den Warenkorb. Nicht zum Kauf erhältlich"),PRODUCTS[0])["status"],"OUT_OF_STOCK")
    def test_recommended_button_not_detected(self):
        p=PRODUCTS[0]
        self.assertNotEqual(classify(fixture(p,"Mehr erfahren")+"<button>In den Warenkorb</button>",p)["status"],"IN_STOCK")
    def test_raw_html_evidence(self):
        r=classify(fixture(PRODUCTS[0],"<span>Nicht zum Kauf erhältlich</span>"),PRODUCTS[0])
        self.assertIn("Nicht zum Kauf erhältlich",r["signals"][0]["html"])
    def test_disc_only_and_no_duplicate(self):
        with TemporaryDirectory() as d, patch("monitor.DATA",Path(d)/"data.json"),patch("monitor.time.sleep"):
            def get(url):
                p=next(p for p in PRODUCTS if p["url"]==url)
                return fixture(p,"In den Warenkorb"),{"http_status":200,"final_url":url,"content_bytes":3000,"sha256":"xyz"}
            with patch("monitor.fetch",side_effect=get),patch("monitor.telegram",return_value="sent") as tg:
                result=once(pause=0)
                self.assertEqual(tg.call_count,2)
                self.assertEqual(result["latest"]["digital-two"]["status"],"IN_STOCK")
                once(pause=0)
                self.assertEqual(tg.call_count,2)

if __name__=="__main__": unittest.main()
