from setuptools import find_packages, setup

package_name = "cast_client"

setup(
    name=package_name,
    version="0.0.1",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="jetcobot",
    maintainer_email="jetcobot@todo.todo",
    description="Python client for cast_move servers",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "cast_client = cast_client.cast_client:main",
        ],
    },
)
