#!/usr/bin/env python3

# used source: https://github.com/FreeOpcUa/python-opcua/blob/master/examples/server-minimal.py
# used source: https://pymodbus.readthedocs.io/en/latest/readme.html#example-code

from pymodbus.client.sync import ModbusTcpClient
from opcua import ua, Server
import yaml
import time
import sys

sys.path.insert(0, "..")


type_dic = {
    "float": ua.VariantType.Float,
    "int": ua.VariantType.Int32,
    "byte": ua.VariantType.Byte,
    "word": ua.VariantType.Int16,
    "boolean": ua.VariantType.Boolean,
}

var_dic = {}
modbus_poll_address = 0
modbus_last_address = 0

# recursive function for adding of nodes from address-config.yaml to OPC UA Server
def add_nodes(config_nodes, mount_node):
    global modbus_poll_address
    global modbus_last_address

    for config_node in config_nodes:
        if "object_node" in config_node:
            object_node = mount_node.add_object(idx, config_node["object_node"]["name"])
            if "nodes" in config_node["object_node"]:
                add_nodes(config_node["object_node"]["nodes"], object_node)
        if "variable_node" in config_node:
            var_config = config_node["variable_node"]
            ma = var_config.get("modbus_address")
            if modbus_poll_address == 0 or ma < modbus_poll_address:
                modbus_poll_address = ma
            if modbus_last_address < ma:
                modbus_last_address = ma
            variable_node = mount_node.add_variable(
                "s=" + var_config["name"],
                var_config["name"],
                0,
                type_dic[var_config["type"]],
            )

            if var_config.get("opc_writable"):
                variable_node.set_writable()
            # save variable with coresponding config info for cyclic updates
            var_dic.update({variable_node: var_config})


if __name__ == "__main__":

    with open("adress_config.yaml", "r") as stream:
        try:
            config = yaml.safe_load(stream)
        except yaml.YAMLError as exc:
            print(exc)

    # setup our server
    opcua_server = Server()
    opcua_server.set_endpoint(
        "opc.tcp://"
        + config["opcua_server"]["ip"]
        + ":"
        + config["opcua_server"]["port"]
        + "/rpi_server/"
    )

    # setup our own namespace, not really necessary but should as spec
    uri = "http://examples.modbustcp"
    idx = opcua_server.register_namespace(uri)

    # get Objects node, this is where we should put our nodes
    objects = opcua_server.get_objects_node()

    # populating our address space
    add_nodes(config["nodes"], objects)

    # starting!
    opcua_server.start()
    print("OPC UA Server running on: ", opcua_server.endpoint.geturl())

    # setup Modbus TCP Client
    modbustcp_client = ModbusTcpClient(
        config["modbustcp_remote_server"]["ip"],
        port=config["modbustcp_remote_server"]["port"],
    )
    print(
        "Modbus TCP Client running on:",
        config["modbustcp_remote_server"]["ip"],
        ":",
        config["modbustcp_remote_server"]["port"],
    )

    register_count = modbus_last_address - modbus_poll_address + 1

    print(
        f"Polling {register_count} modbus registers. Start address: {modbus_poll_address}"
    )

    try:
        while True:

            try:
                result = modbustcp_client.read_holding_registers(
                    modbus_poll_address, register_count
                )
            except Exception as ex:
                print(ex)
                continue

            if isinstance(result, Exception):
                print(result)
                continue

            for opc_var, node_config in var_dic.items():
                if node_config["modbus_type"] == "holding_register":

                    register_index = node_config["modbus_address"] - modbus_poll_address
                    register_data = result.registers[register_index]
                    if hasattr(result, "message"):
                        # modbus tcp communication error
                        print(result.message)
                    else:
                        if node_config["type"] == "boolean":
                            modbus_value = bool(
                                register_data & (0b1 << node_config["modbus_bit"])
                            )
                        elif node_config["type"] == "byte":
                            low_byte = node_config["modbus_bit"] == 0
                            if low_byte:
                                modbus_value = register_data & 0xFF
                            else:  # high byte
                                modbus_value = register_data >> 8
                        else:
                            modbus_value = register_data
                        # print(f"Read from plc {node_config['name']} {modbus_value}")
                        opc_value = opc_var.get_value()
                        old_value = node_config.get("old_value")
                        if old_value != modbus_value:
                            node_config["old_value"] = modbus_value
                            print(node_config["name"], modbus_value)
                            opc_var.set_value(modbus_value)
                        elif old_value != opc_value:
                            node_config["old_value"] = opc_value
                            print(
                                f"Value change detected in opc: {node_config['name']}={repr(opc_value)}"
                            )
                            if node_config["type"] == "boolean":
                                temp_value = register_data
                                if opc_value:
                                    temp_value = temp_value | (
                                        0b1 << node_config["modbus_bit"]
                                    )
                                else:
                                    temp_value = temp_value & ~(
                                        0b1 << node_config["modbus_bit"]
                                    )
                                opc_value = temp_value
                            modbustcp_client.write_registers(
                                node_config["modbus_address"], [opc_value]
                            )

                else:
                    print("Error: only read of holding_register implemented")
            time.sleep(config["polling_cycle_seconds"])
    finally:
        # close connection, remove subscriptions, etc
        opcua_server.stop()
        modbustcp_client.close()
