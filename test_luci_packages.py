import asyncio
import logging
from custom_components.openwrt.api.luci_rpc import LuciRpcClient

logging.basicConfig(level=logging.DEBUG)

async def test_luci_packages():
    client = LuciRpcClient(
        host="192.168.1.1",
        username="root",
        password="Zorneding2603!!",
        use_ssl=False
    )
    
    try:
        await client.connect()
        print("Connected successfully via LuCI RPC")
        
        # Test 1: Original detection logic (sys.exec)
        cmd = (
            "for f in /etc/init.d/sqm /etc/init.d/mwan3 /usr/bin/iwinfo "
            "/usr/bin/etherwake /usr/bin/wg /usr/sbin/openvpn "
            "/usr/lib/lua/luci/controller/rpc.lua; do "
            "if [ -f $f ] || [ -x $f ]; then echo 1; else echo 0; fi; done"
        )
        out = await client._rpc_call("sys", "exec", [cmd])
        print(f"Original detection results: {out}")
        
        # Test 2: UCI fallback (sqm)
        try:
            sqm_uci = await client._rpc_call("uci", "get_all", ["sqm"])
            print(f"SQM UCI check: {'Found' if sqm_uci else 'Not Found'}")
        except Exception as e:
            print(f"SQM UCI check failed: {e}")

        # Test 3: UCI fallback (mwan3)
        try:
            mwan3_uci = await client._rpc_call("uci", "get_all", ["mwan3"])
            print(f"MWAN3 UCI check: {'Found' if mwan3_uci else 'Not Found'}")
        except Exception as e:
            print(f"MWAN3 UCI check failed: {e}")
            
        packages = await client.check_packages()
        print(f"Packages detected: {packages.__dict__}")
        
        await client.disconnect()
    except Exception as e:
        print(f"Connection failed: {e}")

if __name__ == "__main__":
    asyncio.run(test_luci_packages())
