def test_all_exports_importable():
    from agent_kernel import testing

    for name in testing.__all__:
        assert hasattr(testing, name)
