from setuptools import find_packages, setup

setup(
    name="aegis-engine",
    version="0.1.0",
    description="Binance USDT-M trading engine for demo/live trading",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    python_requires=">=3.9",
    install_requires=[
        "ccxt>=4.4.84",
        "pandas>=2.2.0",
        "pyyaml>=6.0.1",
        "python-dotenv>=1.0.1",
        "websocket-client>=1.8.0",
    ],
    entry_points={"console_scripts": ["aegis-engine=aegis_engine.main:main"]},
)
