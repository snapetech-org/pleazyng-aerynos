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


if __name__ == '__main__':
    unittest.main()
