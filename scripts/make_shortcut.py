"""Build the iPhone Shortcut "To FinanceAgent" and sign it, so it can be installed from the app.

    python scripts/make_shortcut.py                     # -> static/FinanceAgent.shortcut
    python scripts/make_shortcut.py --url URL --token T --out /tmp/test.shortcut   # prefilled, for testing

The Shortcut shows up in the share sheet for images, PDFs and files and sends each one to
POST /api/upload with the person's upload token. When it is added, Shortcuts asks for the upload address
and the token (both shown under Family in the app); a test build can prefill them.
Signing needs macOS (`shortcuts sign`); iPhones only install signed Shortcuts.
"""

from __future__ import annotations

import argparse
import plistlib
import subprocess
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OBJ = "￼"  # where a variable sits inside a text field


def text_with(template: str, *outputs: tuple[str, str]) -> dict:
    """A text field with action outputs at each OBJ, e.g. ("Bearer " + OBJ, (uuid, "Token"))."""
    ranges, start = {}, 0
    for output_uuid, name in outputs:
        start = template.index(OBJ, start)
        ranges[f"{{{start}, 1}}"] = {"OutputUUID": output_uuid, "OutputName": name, "Type": "ActionOutput"}
        start += 1
    return {"Value": {"string": template, "attachmentsByRange": ranges}, "WFSerializationType": "WFTextTokenString"}


def plain(text: str) -> dict:
    return {"Value": {"string": text}, "WFSerializationType": "WFTextTokenString"}


def file_variable(name: str) -> dict:
    """A variable as the value of a form field of type File."""
    return {"Value": {"Type": "Variable", "VariableName": name}, "WFSerializationType": "WFTokenAttachmentParameterState"}


def build(url: str, token: str) -> dict:
    address, key, response = (str(uuid.uuid4()).upper() for _ in range(3))
    loop = str(uuid.uuid4()).upper()
    actions = [
        {"WFWorkflowActionIdentifier": "is.workflow.actions.gettext",
         "WFWorkflowActionParameters": {"UUID": address, "CustomOutputName": "Upload address", "WFTextActionText": url}},
        {"WFWorkflowActionIdentifier": "is.workflow.actions.gettext",
         "WFWorkflowActionParameters": {"UUID": key, "CustomOutputName": "Upload token", "WFTextActionText": token}},
        {"WFWorkflowActionIdentifier": "is.workflow.actions.repeat.each",
         "WFWorkflowActionParameters": {"GroupingIdentifier": loop, "WFControlFlowMode": 0,
                                        "WFInput": {"Value": {"Type": "ExtensionInput"},
                                                    "WFSerializationType": "WFTextTokenAttachment"}}},
        {"WFWorkflowActionIdentifier": "is.workflow.actions.downloadurl",
         "WFWorkflowActionParameters": {
             "UUID": response, "CustomOutputName": "FinanceAgent reply",
             "WFURL": text_with(OBJ, (address, "Upload address")),
             "WFHTTPMethod": "POST", "WFHTTPBodyType": "Form", "ShowHeaders": True,
             "WFHTTPHeaders": {"Value": {"WFDictionaryFieldValueItems": [
                 {"WFItemType": 0, "WFKey": plain("Authorization"),
                  "WFValue": text_with("Bearer " + OBJ, (key, "Upload token"))}]},
                 "WFSerializationType": "WFDictionaryFieldValue"},
             "WFFormValues": {"Value": {"WFDictionaryFieldValueItems": [
                 {"WFItemType": 5, "WFKey": plain("file"), "WFValue": file_variable("Repeat Item")},
                 # the token a second time, as a form field: the server accepts either
                 {"WFItemType": 0, "WFKey": plain("token"), "WFValue": text_with(OBJ, (key, "Upload token"))}]},
                 "WFSerializationType": "WFDictionaryFieldValue"}}},
        {"WFWorkflowActionIdentifier": "is.workflow.actions.notification",
         "WFWorkflowActionParameters": {"WFNotificationActionTitle": "FinanceAgent",
                                        "WFNotificationActionBody": text_with(OBJ, (response, "FinanceAgent reply")),
                                        "WFNotificationActionSound": False}},
        {"WFWorkflowActionIdentifier": "is.workflow.actions.repeat.each",
         "WFWorkflowActionParameters": {"GroupingIdentifier": loop, "WFControlFlowMode": 2}},
    ]
    return {
        "WFWorkflowName": "To FinanceAgent",
        "WFWorkflowClientVersion": "2607.0.3",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowIcon": {"WFWorkflowIconStartColor": 463140863, "WFWorkflowIconGlyphNumber": 59511},
        "WFWorkflowTypes": ["ActionExtension"],
        "WFWorkflowHasShortcutInputVariables": True,
        "WFWorkflowInputContentItemClasses": ["WFImageContentItem", "WFPDFContentItem", "WFGenericFileContentItem"],
        "WFWorkflowOutputContentItemClasses": [],
        "WFQuickActionSurfaces": [],
        "WFWorkflowImportQuestions": [
            {"ActionIndex": 0, "Category": "Parameter", "ParameterKey": "WFTextActionText", "DefaultValue": url,
             "Text": "Your upload address (FinanceAgent → Family → iPhone Shortcut)"},
            {"ActionIndex": 1, "Category": "Parameter", "ParameterKey": "WFTextActionText", "DefaultValue": token,
             "Text": "Your upload token (FinanceAgent → Family → Create my upload token)"},
        ],
        "WFWorkflowActions": actions,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="https://your-app.onrender.com/api/upload")
    parser.add_argument("--token", default="")
    parser.add_argument("--out", default=str(ROOT / "static" / "FinanceAgent.shortcut"))
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        unsigned = Path(tmp) / "unsigned.shortcut"
        unsigned.write_bytes(plistlib.dumps(build(args.url, args.token), fmt=plistlib.FMT_BINARY))
        subprocess.run(["shortcuts", "sign", "--mode", "anyone", "--input", str(unsigned), "--output", args.out],
                       check=True)
    print(f"signed Shortcut written to {args.out}")


if __name__ == "__main__":
    main()
