import unittest
import public_install


class PublicInstallTests(unittest.TestCase):
    def receipt(self):
        return {'source_sha': 'a' * 40, 'status': 'qualified', 'version': '3.0.30+189', 'release': 4,
                'tag': 'main-' + 'a' * 12 + '-r4',
                'assets': {'feed.index': 'b' * 64, 'stone.index': 'c' * 64}}

    def test_public_test_requires_exact_qualified_source_version_and_index_digests(self):
        good = self.receipt()
        self.assertTrue(public_install.validate_receipt(good, 'a' * 40).endswith('/stone.index'))
        for changes in [{'source_sha': 'b' * 40}, {'status': 'failed'}, {'tag': 'other'},
                        {'release': True}, {'assets': {}}, {'version': 'main'}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                public_install.validate_receipt({**good, **changes}, 'a' * 40)

    def test_native_moss_result_must_contain_exact_single_installed_package(self):
        public_install.installed_version('other 1.0\npleazyng 3.0.30-4 x86_64\n', self.receipt())
        for output in ['', 'pleazyng 3.0.29-4', 'pleazyng 3.0.30-3',
                       'pleazyng 3.0.30-4\npleazyng 3.0.30-4']:
            with self.subTest(output=output), self.assertRaises(RuntimeError):
                public_install.installed_version(output, self.receipt())

    def test_public_launch_chooses_start_fresh_before_waiting_for_sign_in(self):
        class App:
            def __init__(self):
                self.actions = []

            def ui(self, action='dump', name=''):
                self.actions.append((action, name))
                if action == 'dump':
                    return [{'name': 'Import an existing media library?'},
                            {'name': 'Start fresh'}, {'name': 'Choose a database'}]

            def wait(self, name):
                self.actions.append(('wait', name))

        app = App()
        choose = lambda controls: 'Start fresh' if 'Start fresh' in controls else None
        public_install.connect_to_jellyfin(app, choose)
        self.assertEqual(app.actions, [('dump', ''), ('click', 'Start fresh'), ('wait', 'Connect to Jellyfin')])

    def test_existing_public_setup_waits_without_starting_fresh(self):
        class App:
            def __init__(self):
                self.actions = []

            def ui(self, action='dump', name=''):
                self.actions.append((action, name))
                return [{'name': 'Connect to Jellyfin'}]

            def wait(self, name):
                self.actions.append(('wait', name))

        app = App()
        choose = lambda controls: 'Start fresh' if 'Start fresh' in controls else None
        public_install.connect_to_jellyfin(app, choose)
        self.assertEqual(app.actions, [('dump', ''), ('wait', 'Connect to Jellyfin')])


if __name__ == '__main__':
    unittest.main()
