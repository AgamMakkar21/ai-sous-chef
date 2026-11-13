"""Compile the isolated monitoring deployment without Azure credentials."""

import json
import os
import unittest
from typing import Any, ClassVar
from unittest.mock import patch

from test_foundation import ROOT, compile_bicep


class OperationsTests(unittest.TestCase):
    template: ClassVar[dict[str, Any]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.template = compile_bicep("infra/operations.bicep")

    def test_only_a_workbook_and_alerts_are_created(self) -> None:
        resources = self.template["resources"]
        if isinstance(resources, dict):
            resources = list(resources.values())
        self.assertEqual(
            {resource["type"] for resource in resources if not resource.get("existing")},
            {"Microsoft.Insights/workbooks", "Microsoft.Insights/scheduledQueryRules"},
        )
        self.assertEqual(
            {resource["type"] for resource in resources if resource.get("existing")},
            {"Microsoft.OperationalInsights/workspaces", "Microsoft.Insights/components"},
        )
        self.assertNotIn("Microsoft.Authorization", json.dumps(self.template))

    def test_live_routing_requires_an_explicit_approval_and_existing_destination(self) -> None:
        routing = self.template["parameters"]["routing"]
        self.assertEqual(routing["defaultValue"], {"mode": "disabled"})
        definitions = json.dumps(self.template.get("definitions", {}))
        self.assertIn("reviewReference", definitions)
        self.assertIn("actionGroupResourceIds", definitions)
        self.assertIn('"minLength": 1', definitions)
        resources = self.template["resources"]
        values = resources.values() if isinstance(resources, dict) else resources
        alerts = next(r for r in values if r["type"].endswith("scheduledQueryRules"))
        properties = alerts["properties"]
        self.assertIn("activeSignals", properties["enabled"])
        self.assertIn("approved", self.template["variables"]["activeSignals"])
        self.assertEqual(self.template["parameters"]["enabledSignals"]["defaultValue"], [])
        self.assertTrue(properties["autoMitigate"])
        self.assertFalse(properties["skipQueryValidation"])
        self.assertIn("reviewReference", properties["actions"]["customProperties"])
        self.assertIn("runbookUrl", properties["actions"]["customProperties"])

    def test_every_signal_has_a_numeric_measure_and_existing_runbook_anchor(self) -> None:
        signals = json.loads((ROOT / "monitoring/signals.json").read_text())
        runbook = (ROOT / "docs/operations.md").read_text()
        self.assertEqual(len({signal["id"] for signal in signals}), len(signals))
        for signal in signals:
            self.assertGreater(signal["threshold"], 0)
            self.assertIn(f"## {signal['runbook'].replace('-', ' ').capitalize()}", runbook)
        compiled = json.dumps(self.template)
        self.assertIn("metricMeasureColumn", compiled)
        self.assertIn("Value", compiled)
        self.assertIn("GreaterThanOrEqual", compiled)
        self.assertIn("workspace", compiled)
        self.assertEqual(self.template["parameters"]["modelPrices"]["defaultValue"], {})

    def test_dev_and_prod_are_independently_scoped_and_disabled(self) -> None:
        with patch.dict(
            os.environ,
            {"ASC_AZURE_LOCATION": "synthetic-region", "ASC_RESOURCE_OWNER": "synthetic-owner"},
        ):
            for environment in ("dev", "prod"):
                parameters = compile_bicep(
                    f"infra/environments/{environment}-operations.bicepparam", "build-params"
                )["parameters"]
                self.assertEqual(parameters["environment"]["value"], environment)
                self.assertEqual(parameters["routing"]["value"], {"mode": "disabled"})
                self.assertIn(environment, parameters["workspaceName"]["value"])
                self.assertIn(environment, parameters["applicationInsightsName"]["value"])
