import os, booking_server as A
if os.path.exists(A.DB): os.remove(A.DB)
A.init_db(); A.seed_bookings()
c = A.app.test_client()
print("resources:", [r["name"] for r in c.get("/resources").get_json()])
print("bookings :", [(b["event_name"], b["resource_names"]) for b in c.get("/bookings").get_json()])
print()
for d in c.get("/demo-requests").get_json():
    r = c.post("/bookings/check", json=d).get_json()
    tag = "OK   " if r["ok"] else "CLASH"
    print(tag, d["event_name"], "->", [x["resource"] for x in r["conflicts"]], "| slots:", [s["start"][5:] for s in r["suggestions"]])
print()
good = {"event_name":"Test Event","requester":"me","resource_ids":[2],"start":A.at(4,10),"end":A.at(4,12)}
print("save free slot:", c.post("/bookings", json=good).status_code)
print("save again    :", c.post("/bookings", json=good).status_code, "(409 matlab clash pakda)")
print("bad input     :", c.post("/bookings/check", json={"start":"x"}).status_code)
