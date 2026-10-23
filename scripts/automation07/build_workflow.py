# ruff: noqa: E501
"""Build one isolated n8n 07 draft. Existing workflows are never updated."""

import copy
import json
import uuid
from pathlib import Path

from samples import sample

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs/automation07"
nodes = []
edges = []
DB = {"supabaseApi": {"id": "lM8B6u43Kz4cMryp", "name": "SAEED Apartment DB"}}
BASE = "https://rsqgwepnowpnpealrzbn.supabase.co/rest/v1/rpc/"
SITE = "https://luxurysmartapartments.com/integrations/morning07/"


def add(name, kind, parameters, position, version=2, credentials=None, **settings):
    n = {
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, "lsa07-v1:" + name)),
        "name": name,
        "type": "n8n-nodes-base." + kind,
        "typeVersion": version,
        "parameters": parameters,
        "position": position,
        **settings,
    }
    if credentials:
        n["credentials"] = credentials
    nodes.append(n)
    return name


def edge(a, b, output=0):
    edges.append((a, b, output, 0))


def code(name, body, pos):
    return add(name, "code", {"mode": "runOnceForAllItems", "jsCode": body}, pos)


def rpc(name, fn, body, pos):
    return add(
        name,
        "httpRequest",
        {
            "method": "POST",
            "url": BASE + fn,
            "authentication": "predefinedCredentialType",
            "nodeCredentialType": "supabaseApi",
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": body,
            "options": {"response": {"response": {"responseFormat": "json"}}, "timeout": 30000},
        },
        pos,
        4.5,
        DB,
    )


def gate(name, expression, pos):
    return add(
        name,
        "if",
        {
            "conditions": {
                "options": {
                    "caseSensitive": True,
                    "leftValue": "",
                    "typeValidation": "strict",
                    "version": 2,
                },
                "conditions": [
                    {
                        "leftValue": expression,
                        "rightValue": True,
                        "operator": {"type": "boolean", "operation": "equals"},
                    }
                ],
                "combinator": "and",
            },
            "options": {},
        },
        pos,
        2.3,
    )


def response(name, status, body, pos):
    return add(
        name,
        "respondToWebhook",
        {"respondWith": "json", "responseBody": body, "options": {"responseCode": status}},
        pos,
        1.5,
    )


add(
    "M00 Morning07 Website Event",
    "webhook",
    {
        "httpMethod": "POST",
        "path": "lsa07-morning-stay-v1",
        "responseMode": "responseNode",
        "options": {},
    },
    [-1100, 0],
    2.2,
)
code(
    "M01 Validate Event Reference",
    r"""const b=$json.body||{};const valid=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(b.event_id||'')&&typeof b.event_token==='string'&&b.event_token.length>=40&&b.event_token.length<=300;return [{json:valid?{valid:true,event_id:b.event_id,event_token:b.event_token}:{valid:false,error:'invalid_event_reference'}}];""",
    [-850, 0],
)
gate("M01A Valid Reference?", "={{ $json.valid === true }}", [-600, 0])
response(
    "M01R Reject Invalid Reference",
    400,
    '={{ {accepted:false,error:"invalid_event_reference"} }}',
    [-350, 240],
)
add(
    "M02 Read Authoritative Website Event",
    "httpRequest",
    {
        "url": "={{ '" + SITE + "events/' + $json.event_id + '/' }}",
        "sendHeaders": True,
        "headerParameters": {
            "parameters": [{"name": "X-LSA-Morning07-Token", "value": "={{ $json.event_token }}"}]
        },
        "options": {
            "redirect": {"redirect": {"followRedirects": False}},
            "response": {
                "response": {"fullResponse": True, "neverError": True, "responseFormat": "json"}
            },
            "timeout": 20000,
        },
    },
    [-350, 0],
    4.5,
    onError="continueRegularOutput",
)
code(
    "M03 Validate Verified Morning Booking",
    r"""const d=$json.body||{};const uuid=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;const valid=$json.statusCode===200&&d.source==='morning07'&&d.payment_verified===true&&['paid','cancelled'].includes(d.status)&&uuid.test(d.morning_stay_id||'')&&uuid.test(d.source_event_id||'')&&Number.isSafeInteger(d.source_revision)&&d.source_revision>0&&Number.isSafeInteger(d.listing_map_id)&&d.listing_map_id>0&&/^\+?[1-9]\d{7,14}$/.test(d.phone||'')&&/^[0-9a-f]{64}$/.test(d.state_fingerprint||'')&&typeof d.delivery_token==='string'&&d.delivery_token.length>=40&&d.arrival_date===d.departure_date&&['Asia/Riyadh','Africa/Casablanca'].includes(d.timezone)&&d.check_in_time>'00:00'&&d.check_in_time<d.check_out_time&&d.check_out_time<'12:00'&&Date.parse(d.starts_at)<Date.parse(d.ends_at);return [{json:valid?{...d,valid:true,reservation_key:'morning07:'+d.morning_stay_id}:{valid:false,error:'website_admission_not_verified'}}];""",
    [-100, 0],
)
gate("M04 Admission Verified?", "={{ $json.valid === true }}", [150, 0])
response(
    "M04R Reject Unverified Admission",
    422,
    '={{ {accepted:false,error:"website_admission_not_verified"} }}',
    [400, 240],
)
rpc("M05 Persist Morning07 Revision", "lsa07_ingest", "={{ {p_snapshot:$json} }}", [400, 0])
code(
    "M06 Build Durable Acknowledgement",
    "if($json.accepted!==true)throw Error('Morning07 persistence not confirmed');return [{json:{accepted:true,event_id:$('M01 Validate Event Reference').first().json.event_id}}];",
    [650, 0],
)
response("M07 Acknowledge Persisted Event", 202, "={{ $json }}", [900, 0])
for a, b in [
    ("M00 Morning07 Website Event", "M01 Validate Event Reference"),
    ("M01 Validate Event Reference", "M01A Valid Reference?"),
    ("M01A Valid Reference?", "M02 Read Authoritative Website Event"),
    ("M02 Read Authoritative Website Event", "M03 Validate Verified Morning Booking"),
    ("M03 Validate Verified Morning Booking", "M04 Admission Verified?"),
    ("M04 Admission Verified?", "M05 Persist Morning07 Revision"),
    ("M05 Persist Morning07 Revision", "M06 Build Durable Acknowledgement"),
    ("M06 Build Durable Acknowledgement", "M07 Acknowledge Persisted Event"),
]:
    edge(a, b)
edge("M01A Valid Reference?", "M01R Reject Invalid Reference", 1)
edge("M04 Admission Verified?", "M04R Reject Unverified Admission", 1)

add(
    "T00 Morning07 Independent Minute Tick",
    "scheduleTrigger",
    {"rule": {"interval": [{"field": "minutes", "minutesInterval": 1}]}},
    [-1100, 650],
    1.4,
)
rpc(
    "B00 Claim Pending Morning Booking",
    "lsa07_claim_booking",
    "={{ {p_worker:String($execution.id)} }}",
    [-850, 650],
)
gate("B01 Booking Preparation Pending?", "={{ $json.has_work === true }}", [-600, 650])
render = (ROOT / "scripts/automation07/render.cjs").read_text().split("if(typeof module")[0]
code(
    "B02 Render Morning Messages and Exit Reminder",
    render + "\nreturn [{json:render($json)}];",
    [-350, 650],
)
rpc(
    "B03 Persist Independent Morning Jobs",
    "lsa07_prepare",
    "={{ {p_stay:$json.stay_id,p_revision:$json.revision,p_worker:String($execution.id),p_jobs:$json.jobs} }}",
    [-100, 650],
)
edge("T00 Morning07 Independent Minute Tick", "B00 Claim Pending Morning Booking")
edge("M07 Acknowledge Persisted Event", "B00 Claim Pending Morning Booking")
edge("B00 Claim Pending Morning Booking", "B01 Booking Preparation Pending?")
edge("B01 Booking Preparation Pending?", "B02 Render Morning Messages and Exit Reminder")
edge("B02 Render Morning Messages and Exit Reminder", "B03 Persist Independent Morning Jobs")

rpc(
    "D00 Claim Next Morning07 Delivery",
    "lsa07_next",
    "={{ {p_worker:String($execution.id)} }}",
    [200, 650],
)
conditions = []
for value in ["send", "check", "wait"]:
    conditions.append(
        {
            "conditions": {
                "options": {
                    "caseSensitive": True,
                    "leftValue": "",
                    "typeValidation": "strict",
                    "version": 2,
                },
                "conditions": [
                    {
                        "leftValue": "={{ $json.action }}",
                        "rightValue": value,
                        "operator": {"type": "string", "operation": "equals"},
                    }
                ],
                "combinator": "and",
            },
            "renameOutput": True,
            "outputKey": value,
        }
    )
add(
    "D01 Route Morning Delivery Action",
    "switch",
    {"rules": {"values": conditions}, "options": {}},
    [450, 650],
    3.2,
)
options = {
    "response": {"response": {"fullResponse": True, "neverError": True, "responseFormat": "json"}},
    "timeout": 30000,
}
headers = {
    "parameters": [{"name": "X-LSA-Morning07-Token", "value": "={{ $json.delivery_token }}"}]
}
add(
    "D10 Send via Morning07 Website Gateway",
    "httpRequest",
    {
        "method": "POST",
        "url": SITE + "send/",
        "sendHeaders": True,
        "headerParameters": headers,
        "sendBody": True,
        "specifyBody": "json",
        "jsonBody": "={{ {job_key:$json.job_key,reference_id:$json.reference_id,message_type:$json.message_type,recipient_kind:$json.recipient_kind,recipient_phone:$json.recipient_phone,message_body:$json.message_body} }}",
        "options": options,
    },
    [750, 450],
    4.5,
    onError="continueRegularOutput",
    retryOnFail=False,
)
add(
    "D20 Read Exact Morning07 Provider Receipt",
    "httpRequest",
    {
        "url": SITE + "receipt/",
        "sendHeaders": True,
        "headerParameters": headers,
        "sendQuery": True,
        "queryParameters": {
            "parameters": [{"name": "reference_id", "value": "={{ $json.reference_id }}"}]
        },
        "options": options,
    },
    [750, 700],
    4.5,
    onError="continueRegularOutput",
)
ev = (ROOT / "scripts/automation07/evidence.cjs").read_text().split("if(typeof module")[0]
code(
    "D30 Interpret Exact Delivery Evidence",
    ev
    + "\nconst job=$('D00 Claim Next Morning07 Delivery').item.json;return [{json:{job_key:job.job_key,result:evidence(job,$json)}}];",
    [1050, 600],
)
rpc(
    "D40 Record Morning07 Delivery Progress",
    "lsa07_result",
    "={{ {p_key:$json.job_key,p_worker:String($execution.id),p_result:$json.result} }}",
    [1300, 600],
)
add(
    "D50 Wait for Receipt Window",
    "wait",
    {"resume": "timeInterval", "amount": "={{ $json.wait_seconds || 15 }}", "unit": "seconds"},
    [750, 950],
    1.1,
)
edge("B03 Persist Independent Morning Jobs", "D00 Claim Next Morning07 Delivery")
edge("T00 Morning07 Independent Minute Tick", "D00 Claim Next Morning07 Delivery")
edge("D00 Claim Next Morning07 Delivery", "D01 Route Morning Delivery Action")
edge("D01 Route Morning Delivery Action", "D10 Send via Morning07 Website Gateway")
edge("D01 Route Morning Delivery Action", "D20 Read Exact Morning07 Provider Receipt", 1)
edge("D01 Route Morning Delivery Action", "D50 Wait for Receipt Window", 2)
edge("D10 Send via Morning07 Website Gateway", "D30 Interpret Exact Delivery Evidence")
edge("D20 Read Exact Morning07 Provider Receipt", "D30 Interpret Exact Delivery Evidence")
edge("D30 Interpret Exact Delivery Evidence", "D40 Record Morning07 Delivery Progress")
edge("D40 Record Morning07 Delivery Progress", "D00 Claim Next Morning07 Delivery")
edge("D50 Wait for Receipt Window", "D00 Claim Next Morning07 Delivery")

rpc(
    "C00 Claim Morning07 Contact Work",
    "lsa07_contact_claim",
    "={{ {p_worker:String($execution.id)} }}",
    [-850, 1400],
)
gate("C01 Contact Work Pending?", "={{ $json.has_contact === true }}", [-600, 1400])
edge("T00 Morning07 Independent Minute Tick", "C00 Claim Morning07 Contact Work")
edge("C00 Claim Morning07 Contact Work", "C01 Contact Work Pending?")
gc = json.loads((ROOT / "scripts/automation07/contact-source.json").read_text())
for n in gc["nodes"]:
    n = copy.deepcopy(n)
    n["id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, "lsa07-v1:" + n["name"]))
    n["position"] = [n["position"][0] - 1500, n["position"][1] + 2200]
    n.pop("webhookId", None)
    nodes.append(n)
for a, types in gc["connections"].items():
    for oi, items in enumerate(types.get("main", [])):
        for item in items:
            edge(a, item["node"], oi)
edge("C01 Contact Work Pending?", "GC00 Prepare English Guest Contact")
rpc(
    "C90 Record Morning07 Contact Outcome",
    "lsa07_contact_result",
    "={{ {p_stay:$('C00 Claim Morning07 Contact Work').first().json.morning_stay_id,p_revision:$('C00 Claim Morning07 Contact Work').first().json.source_revision,p_worker:String($execution.id),p_ok:!$('GC07 Google Contact Failed?').first().json.contact_error} }}",
    [1500, 1800],
)
edge("GC07 Google Contact Failed?", "C90 Record Morning07 Contact Outcome", 1)
edge("GC08 Telegram Contact Save Failure", "C90 Record Morning07 Contact Outcome")

add("E00 Morning07 Unexpected Error", "errorTrigger", {}, [-1100, 2350], 1)
add(
    "E01 Morning07 Technical Alert",
    "telegram",
    {
        "operation": "sendMessage",
        "chatId": "=@n8n_updates_com",
        "text": '={{ "⚠️ Scenario 07 — morning stay automation requires attention. Execution: "+($json.execution?.id||$execution.id) }}',
        "additionalFields": {"appendAttribution": False},
    },
    [-850, 2350],
    1.2,
    {"telegramApi": {"id": "FxT4i1JVLXG3B2O1", "name": "@LSA_Financial_Bot"}},
)
edge("E00 Morning07 Unexpected Error", "E01 Morning07 Technical Alert")

groups = [
    {
        "name": "Verify website payment and booking",
        "nodeNames": [
            "M02 Read Authoritative Website Event",
            "M03 Validate Verified Morning Booking",
        ],
        "description": "Read one signed website event and validate the mandatory phone, payment and morning window.",
    },
    {
        "name": "Durable acknowledgement",
        "nodeNames": [
            "M05 Persist Morning07 Revision",
            "M06 Build Durable Acknowledgement",
            "M07 Acknowledge Persisted Event",
        ],
        "description": "Acknowledge only after the independent 07 reservation revision is committed.",
    },
    {
        "name": "Morning message preparation",
        "nodeNames": [
            "B02 Render Morning Messages and Exit Reminder",
            "B03 Persist Independent Morning Jobs",
        ],
        "description": "Confirmation, house rules, access guide, one work-group notice and exact checkout timing.",
    },
    {
        "name": "Receipts and independent job state",
        "nodeNames": [
            "D30 Interpret Exact Delivery Evidence",
            "D40 Record Morning07 Delivery Progress",
        ],
        "description": "Require exact provider evidence; ambiguous POSTs are reconciled without blind resending.",
    },
]
groups = json.loads((ROOT / "scripts/automation07/groups.json").read_text())
positions = json.loads((ROOT / "scripts/automation07/positions.json").read_text())
for item in nodes:
    if item["name"] in positions:
        item["position"] = positions[item["name"]]
connections = {}
for a, b, oi, ti in edges:
    outs = connections.setdefault(a, {}).setdefault("main", [])
    while len(outs) <= oi:
        outs.append([])
    outs[oi].append({"node": b, "type": "main", "index": ti})
workflow = {
    "name": "Apartment Pathway 07 — Independent Morning Stay Journey",
    "active": False,
    "nodes": nodes,
    "connections": connections,
    "nodeGroups": groups,
    "settings": {"executionOrder": "v1", "timezone": "Asia/Riyadh", "executionTimeout": 900},
}
(OUT / "workflow-bound.json").write_text(json.dumps(workflow, ensure_ascii=False))
share = copy.deepcopy(workflow)
for n in share["nodes"]:
    n.pop("credentials", None)
(OUT / "workflow.json").write_text(json.dumps(share, ensure_ascii=False, indent=2))
handles = {n["name"]: "n" + str(i) for i, n in enumerate(nodes)}


def js(v, key=""):
    if isinstance(v, str):
        if key not in {"jsCode", "functionCode"} and v.startswith("="):
            return "expr(" + json.dumps(v[1:], ensure_ascii=False) + ")"
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ",".join(js(x, key) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ",".join(json.dumps(k) + ":" + js(x, k) for k, x in v.items()) + "}"
    return json.dumps(v)


lines = []
for n in nodes:
    config = {
        k: v
        for k, v in n.items()
        if k not in {"id", "type", "typeVersion", "credentials", "webhookId"}
    }
    config_text = js(config)
    if n.get("credentials"):
        creds = (
            "{"
            + ",".join(
                json.dumps(k) + ":newCredential(" + json.dumps(v["name"]) + ")"
                for k, v in n["credentials"].items()
            )
            + "}"
        )
        config_text = config_text[:-1] + ",credentials:" + creds + "}"
    factory = (
        "trigger"
        if n["type"].split(".")[-1].lower().endswith("trigger") or n["type"].endswith(".webhook")
        else "node"
    )
    lines.append(
        "const "
        + handles[n["name"]]
        + " = "
        + factory
        + "({type:"
        + json.dumps(n["type"])
        + ",version:"
        + str(n["typeVersion"])
        + ",config:"
        + config_text
        + ",output:[{json:"
        + js(sample(n["name"]))
        + "}]});"
    )
lines.append(
    "export default workflow('lsa07-morning-stays','Apartment Pathway 07 — Independent Morning Stay Journey')"
)
for n in nodes:
    lines.append(".add(" + handles[n["name"]] + ")")
for a, b, oi, ti in edges:
    lines.append(
        ".add("
        + handles[a]
        + ".output("
        + str(oi)
        + ").to("
        + handles[b]
        + ".input("
        + str(ti)
        + ")))"
    )
for g in groups:
    lines.append(
        ".group("
        + json.dumps(g["name"])
        + ",["
        + ",".join(handles[x] for x in g["nodeNames"])
        + "],{description:"
        + json.dumps(g["description"])
        + "})"
    )
lines[-1] += ";"
(OUT / "workflow-sdk.js").write_text("\n".join(lines))
print(
    json.dumps(
        {
            "nodes": len(nodes),
            "edges": len(edges),
            "sdk_bytes": (OUT / "workflow-sdk.js").stat().st_size,
            "no_phone_recovery": not any("formTrigger" in n["type"] for n in nodes),
        }
    )
)
