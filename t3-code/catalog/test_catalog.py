import unittest
from unittest.mock import patch
from catalog import fetch_inventory, translate


class CatalogTest(unittest.TestCase):
    def test_deployment_aliases_and_metadata_filter(self):
        templates = {'models': [{'slug': 'gpt-6-luna', 'context_window': 123}]}
        deployments = [
            {'type': 'ModelDeployment', 'name': 'my-luna', 'modelName': 'gpt-6-luna', 'capabilities': {'chat_completion': 'true'}},
            {'type': 'ModelDeployment', 'name': 'embedding', 'modelName': 'embedding', 'capabilities': {'embeddings': 'true'}},
            {'type': 'ModelDeployment', 'name': 'unknown-chat', 'modelName': 'unknown', 'capabilities': {'chat_completion': 'true'}},
        ]
        result, omitted = translate(deployments, templates)
        self.assertEqual(result, {'models': [{'slug': 'my-luna', 'context_window': 123, 'display_name': 'foundry-my-luna'}]})
        self.assertEqual(omitted, ['unknown-chat'])
        self.assertEqual(templates['models'][0]['slug'], 'gpt-6-luna')
        self.assertEqual(translate([], templates), ({'models': []}, []))

    def test_refuses_foreign_pagination_before_sending_key(self):
        project = 'https://project.services.ai.azure.com/api/projects/test'
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): pass
            def read(self): return b'{"value": [], "nextLink": "https://other.example/deployments"}'
        with patch('catalog.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = Response()
            with self.assertRaisesRegex(ValueError, 'outside'):
                fetch_inventory(project, 'test-token')
            self.assertEqual(opener.return_value.open.call_count, 1)


if __name__ == '__main__':
    unittest.main()
