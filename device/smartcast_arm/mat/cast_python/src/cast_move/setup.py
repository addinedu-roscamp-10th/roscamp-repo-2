from setuptools import find_packages, setup

package_name = "cast_move"

setup(
    name=package_name,
    version="0.0.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="jetcobot",
    maintainer_email="jetcobot@todo.todo",
    description="Python action servers for casting robot motions",
    license="TODO: License declaration",
    entry_points={
        "console_scripts": [
            "return_to_base_server = cast_move.servers:return_to_base_main",
            "pattern_pick_server = cast_move.servers:pattern_pick_main",
            "die_stamp_server = cast_move.servers:die_stamp_main",
            "pattern_return_server = cast_move.servers:pattern_return_main",
            "crucible_pick_server = cast_move.servers:crucible_pick_main",
            "metal_pour_server = cast_move.servers:metal_pour_main",
            "crucible_return_server = cast_move.servers:crucible_return_main",
            "casting_pick_server = cast_move.servers:casting_pick_main",
            "tat_load_server = cast_move.servers:tat_load_main",
            "cast_servers = cast_move.servers:all_servers_main",
        ],
    },
)
