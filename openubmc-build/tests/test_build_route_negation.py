from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from build_route import classify_request


class BuildRouteNegationTests(unittest.TestCase):
    def test_negated_outputs_do_not_request_artifacts_or_publication(self):
        requests = [
            '只编译组件，不生成HPM',
            'Compile and validate a minimal component backport; no package publication or HPM',
            'Compile the component only; do not publish package',
            '编译组件，不发布 conan 包',
            'Compile without producing a product image or rootfs',
            '只运行单元测试，不要上传 conan 包，也不生成固件包',
            '只编译，不要安装构建环境',
        ]
        for request in requests:
            with self.subTest(request=request):
                self.assertEqual(classify_request(request)[:2], ('openubmc-build', 'validate'))

    def test_positive_outputs_after_a_negation_are_preserved(self):
        for request, expected in [
            ('不生成HPM，只生成组件包', ('openubmc-build','component-package')),
            ('不要发布 conan 包，但需要生成HPM', ('openubmc-build','product-artifact')),
            ('Do not publish package, but build an HPM', ('openubmc-build','product-artifact')),
            ('Build HPM; do not upload package', ('openubmc-build','product-artifact')),
            ('发布 conan 包，不生成HPM', ('openubmc-publish','handoff')),
        ]:
            with self.subTest(request=request):
                self.assertEqual(classify_request(request)[:2], expected)

    def test_an_explicit_command_keeps_its_owner(self):
        self.assertEqual(classify_request('do not build an HPM', ['bingo','build'])[:2], ('openubmc-build','unsupported'))
        self.assertEqual(classify_request('no HPM', ['bmcgo','build','--board','example'])[:2], ('openubmc-build','product-artifact'))


if __name__ == '__main__':
    unittest.main()
