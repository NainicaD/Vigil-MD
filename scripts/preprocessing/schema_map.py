# scripts/preprocessing/schema_map.py

ENFORCEMENT_MAP = {
    "source":           lambda r: "enforcement",
    "source_record_id": lambda r: r.get("recall_number"),        # Z-XXXX-YYYY
    "link_key":         lambda r: r.get("recall_number"),        # joins to recall.product_res_number
    "event_id":         lambda r: r.get("event_id"),             # internal FDA ID
    "manufacturer_name_raw": lambda r: r.get("recalling_firm"),
    "device_name_raw":  lambda r: r.get("product_description"),
    "recall_class":     lambda r: r.get("classification"),       # Class I / II / III
    "reason_text":      lambda r: r.get("reason_for_recall"),
    "scope_text":       lambda r: " ".join(filter(None, [
                            r.get("code_info"), r.get("more_code_info")])),
    "record_date":      lambda r: r.get("report_date"),
    "status":           lambda r: r.get("status"),
    "raw_json":         lambda r: r,
}

RECALL_MAP = {
    "source":           lambda r: "recall",
    "source_record_id": lambda r: r.get("product_res_number"),   # Z-XXXX-YY
    "link_key":         lambda r: r.get("product_res_number"),   # joins to enforcement.recall_number
    "res_event_number": lambda r: r.get("res_event_number"),
    "manufacturer_name_raw": lambda r: r.get("recalling_firm"),
    "device_name_raw":  lambda r: r.get("product_description"),
    "product_code":     lambda r: r.get("product_code"),
    "reason_text":      lambda r: r.get("reason_for_recall"),
    "scope_text":       lambda r: r.get("code_info"),
    "record_date":      lambda r: r.get("event_date_posted"),
    "status":           lambda r: r.get("recall_status"),
    "raw_json":         lambda r: r,
}

MAUDE_MAP = {
    "source":           lambda r, d: "maude",
    "source_record_id": lambda r, d: r.get("mdr_report_key"),
    "manufacturer_name_raw": lambda r, d: (
                            d.get("manufacturer_d_name") or r.get("manufacturer_name")),
    "device_name_raw":  lambda r, d: d.get("brand_name"),
    "model_number_raw": lambda r, d: d.get("model_number"),
    "catalog_number":   lambda r, d: d.get("catalog_number"),
    "lot_number":       lambda r, d: d.get("lot_number"),     # already structured
    "udi_di":           lambda r, d: d.get("udi_di"),         # 55% coverage
    "product_code":     lambda r, d: d.get("device_report_product_code"),
    "record_date":      lambda r: r.get("date_received"),
    "raw_json":         lambda r, d: r,
    # Note: expand one row per device in the device[] array
}