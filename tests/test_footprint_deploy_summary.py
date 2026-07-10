import unittest

from tabs.footprint import summarize_deploy_results


class DeploySummaryTests(unittest.TestCase):
    def test_marks_deployment_success_when_all_steps_succeeded_or_skipped(self):
        results = {
            "project_update": {"success": True, "message": "All features updated successfully."},
            "old_footprint_delete": {"success": True, "message": "All features deleted successfully."},
            "locations_delete": {"success": True, "message": "All features deleted successfully."},
            "geography_deletes": {
                "house": {"success": True, "message": "All features deleted successfully."},
                "borough": {"skipped": True, "reason": "No existing borough geography records were found to delete."},
            },
            "new_footprint_add": {"success": True, "message": "All features added successfully."},
            "locations_add": {"success": True, "message": "All features added successfully."},
            "geography_adds": {
                "house": {"success": True, "message": "All features added successfully."},
                "borough": {"skipped": True, "reason": "No new borough geography add payload was built."},
            },
            "traffic_impacts_update": {"success": True, "message": "All features updated successfully."},
        }

        summary = summarize_deploy_results(results)

        self.assertTrue(summary["success"])
        self.assertEqual(summary["message"], "Deployment completed successfully.")

    def test_marks_deployment_failed_when_any_step_failed(self):
        results = {
            "project_update": {"success": True, "message": "All features updated successfully."},
            "old_footprint_delete": {"success": False, "message": "Delete failed."},
        }

        summary = summarize_deploy_results(results)

        self.assertFalse(summary["success"])
        self.assertIn("old_footprint_delete", summary["failed_steps"])


if __name__ == "__main__":
    unittest.main()
