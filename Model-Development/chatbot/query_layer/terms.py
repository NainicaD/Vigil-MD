"""MAUDE problem terms that need special handling in answers.

From the data checks of 2026-11-07 (design doc "Checking the data for the chatbot"):
about 7% of patient terms and about 10% of the top device terms carry no information, and
about half of all patient terms say that no harm was reported.

NO_INFORMATION terms are left out of "top problems". NO_HARM terms are left out of the
ranking too, but their report count is returned separately ("no patient harm reported in
N reports"), because that is real information.
"""
from __future__ import annotations

NO_INFORMATION: tuple[str, ...] = (
    # device
    "Adverse Event Without Identified Device or Use Problem",
    "Insufficient Device Problem Information",
    "Appropriate Device Problem Term/Code Not Available",
    # patient
    "Insufficient Information",
    "No Code Available",
    "No Information",
    "Appropriate Clinical Signs, Symptoms and Conditions Term/Code Not Available",
)

NO_HARM: tuple[str, ...] = (
    "No Clinical Signs, Symptoms or Conditions",
    "No Consequences Or Impact To Patient",
    "No Known Impact Or Consequence To Patient",
    "No Patient Involvement",
)
