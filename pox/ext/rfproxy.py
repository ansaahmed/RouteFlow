import struct

from pox.core import core
from pox.openflow.libopenflow_01 import *
import pymongo as mongo

import rflib.ipc.IPC as IPC
import rflib.ipc.MongoIPC as MongoIPC
from rflib.ipc.RFProtocol import *
from rflib.openflow.rfofmsg import *
from rflib.ipc.rfprotocolfactory import RFProtocolFactory
from rflib.defs import *

FAILURE = 0
SUCCESS = 1

# Association table
class Table:
    def __init__(self):
        self.dp_to_vs = {}
        self.vs_to_dp = {}

    def update_dp_port(self, dp_id, dp_port, vs_id, vs_port):
        # If there was a mapping for this DP port, reset it
        if (dp_id, dp_port) in self.dp_to_vs:
            old_vs_port = self.dp_to_vs[(dp_id, dp_port)]
            del self.vs_to_dp[old_vs_port]
        self.dp_to_vs[(dp_id, dp_port)] = (vs_id, vs_port)
        self.vs_to_dp[(vs_id, vs_port)] = (dp_id, dp_port)

    def dp_port_to_vs_port(self, dp_id, dp_port):
        try:
            return self.dp_to_vs[(dp_id, dp_port)]
        except KeyError:
            return None

    def vs_port_to_dp_port(self, vs_id, vs_port):
        try:
            return self.vs_to_dp[(vs_id, vs_port)]
        except KeyError:
            return None

    # We're not considering the case of this table becoming invalid when a
    # datapath goes down. When the datapath comes back, the server recreates
    # the association, forcing new map messages to be generated, overriding the
    # previous mapping.
    # If a packet comes and matches the invalid mapping, it can be redirected
    # to the wrong places. We have to fix this.

netmask_prefix = lambda a: sum([bin(int(x)).count("1") for x in a.split(".", 4)])
format_id = lambda dp_id: hex(dp_id).rstrip("L")

ipc = MongoIPC.MongoIPCMessageService(MONGO_ADDRESS, MONGO_DB_NAME, RFPROXY_ID)
table = Table()
log = core.getLogger()

# Base methods
def send_of_msg(dp_id, ofmsg):
    topology = core.components['topology']
    switch = topology.getEntityByID(dp_id)
    if switch is not None and switch.connected:
        try:
            switch.send(ofmsg)
        except:
            return FAILURE
        return SUCCESS
    else:
        return FAILURE

def send_packet_out(dp_id, port, data):
    msg = ofp_packet_out()
    msg.actions.append(ofp_action_output(port=port))
    msg.data = data
    msg.in_port = OFPP_NONE
    topology = core.components['topology']
    switch = topology.getEntityByID(dp_id)
    if switch is not None and switch.connected:
        try:
            switch.send(msg)
        except:
            return FAILURE
        return SUCCESS
    else:
        return FAILURE

# Flow installation methods
def flow_config(dp_id, operation_id):
    ofmsg = create_config_msg(operation_id)
    if send_of_msg(dp_id, ofmsg) == SUCCESS:
        log.info("ofp_flow_mod(config) was sent to datapath (dp_id=%s)",
                 format_id(dp_id))
    else:
        log.info("Error sending ofp_flow_mod(config) to datapath (dp_id=%s)",
                 format_id(dp_id))

def flow_add(dp_id, address, netmask, src_hwaddress, dst_hwaddress, dst_port):
    netmask = netmask_prefix(netmask)
    address = address + "/" + str(netmask)
                
    ofmsg = create_flow_install_msg(address, netmask, 
                                    src_hwaddress, dst_hwaddress, 
                                    dst_port)
    if send_of_msg(dp_id, ofmsg) == SUCCESS:
        log.info("ofp_flow_mod(add) was sent to datapath (dp_id=%s)",
                 format_id(dp_id))
    else:
        log.info("Error sending ofp_flow_mod(add) to datapath (dp_id=%s)",
                 format_id(dp_id))

def flow_delete(dp_id, address, netmask, src_hwaddress):
    netmask = netmask_prefix(netmask)
    address = address + "/" + str(netmask)
                
    ofmsg1 = create_flow_remove_msg(address, netmask, src_hwaddress)
    if send_of_msg(dp_id, ofmsg1) == SUCCESS:
        log.info("ofp_flow_mod(delete) was sent to datapath (dp_id=%s)",
                 format_id(dp_id))
    else:
        log.info("Error sending ofp_flow_mod(delete) to datapath (dp_id=%s)",
                 format_id(dp_id))

    ofmsg2 = create_temporary_flow_msg(address, netmask, src_hwaddress)
    if send_of_msg(dp_id, ofmsg2) == SUCCESS:
        log.info("ofp_flow_mod(delete) was sent to datapath (dp_id=%s)",
                 format_id(dp_id))
    else:
        log.info("Error sending ofp_flow_mod(delete) to datapath (dp_id=%s)",
                 format_id(dp_id))
                 
# Event handlers
def on_datapath_up(event):
    log.info("Datapath id=%s is up, installing config flows...", event.dpid)
    topology = core.components['topology']
    dp_id = event.dpid
    
    ports = topology.getEntityByID(dp_id).ports
    for port in ports:
        if port <= OFPP_MAX:
            msg = DatapathPortRegister(dp_id=dp_id, dp_port=port)
            ipc.send(RFSERVER_RFPROXY_CHANNEL, RFSERVER_ID, msg)
            
            log.info("Registering datapath port (dp_id=%s, dp_port=%d)",
                     format_id(dp_id), port)
                      
def on_datapath_down(event):
    dp_id = event.dpid
        
    log.info("Datapath is down (dp_id=%s)", format_id(dp_id))
    msg = DatapathDown(dp_id=dp_id)
    ipc.send(RFSERVER_RFPROXY_CHANNEL, RFSERVER_ID, msg)

def on_packet_in(event):
    packet = event.parsed
    dp_id = event.dpid
    in_port = event.port
    
    # Drop all LLDP packets
    if packet.type == ethernet.LLDP_TYPE:
        return
        
    # If we have a mapping packet, inform RFServer through a Map message
    if packet.type == RF_ETH_PROTO:
        vm_id, vm_port = struct.unpack("QB", packet.raw[14:])

        log.info("Received mapping packet (vm_id=%s, vm_port=%d, vs_id=%s, vs_port=%d)",
                 format_id(vm_id), vm_port, event.dpid, event.port)
        
        msg = VirtualPlaneMap(vm_id=vm_id, vm_port=vm_port,
                              vs_id=event.dpid, vs_port=event.port)
        ipc.send(RFSERVER_RFPROXY_CHANNEL, RFSERVER_ID, msg)
        return

    # If the packet came from RFVS, redirect it to the right switch port
    if event.dpid == RFVS_DPID:
        dp_port = table.vs_port_to_dp_port(dp_id, in_port)
        if dp_port is not None:
            dp_id, dp_port = dp_port
            send_packet_out(dp_id, dp_port, event.data)
        else:
            log.debug("Unmapped RFVS port (vs_id=%s, vs_port=%d)",
                      format_id(dp_id), in_port)
    # If the packet came from a switch, redirect it to the right RFVS port
    else:
        vs_port = table.dp_port_to_vs_port(dp_id, in_port)
        if vs_port is not None:
            vs_id, vs_port = vs_port
            send_packet_out(vs_id, vs_port, event.data)
        else:
            log.debug("Unmapped datapath port (dp_id=%s, dp_port=%d)",
                      format_id(dp_id), in_port)

# IPC message Processing
class RFProcessor(IPC.IPCMessageProcessor):
    def process(self, from_, to, channel, msg):
        topology = core.components['topology']
        type_ = msg.get_type()
        if type_ == DATAPATH_CONFIG:
            flow_config(msg.get_dp_id(), msg.get_operation_id())
        elif type_ == FLOW_MOD:
            if (msg.get_is_removal()):
                flow_delete(msg.get_dp_id(), 
                            msg.get_address(), msg.get_netmask(), 
                            msg.get_src_hwaddress())
            else:
                flow_add(msg.get_dp_id(), 
                         msg.get_address(), msg.get_netmask(), 
                         msg.get_src_hwaddress(), msg.get_dst_hwaddress(), 
                         msg.get_dst_port())
                         
        if type_ == DATA_PLANE_MAP:
            table.update_dp_port(msg.get_dp_id(), msg.get_dp_port(), 
                                 msg.get_vs_id(), msg.get_vs_port())

        return True

# Initialization
def launch ():
    core.openflow.addListenerByName("ConnectionUp", on_datapath_up)
    core.openflow.addListenerByName("ConnectionDown", on_datapath_down)
    core.openflow.addListenerByName("PacketIn", on_packet_in)
    ipc.listen(RFSERVER_RFPROXY_CHANNEL, RFProtocolFactory(), RFProcessor(), False)
    log.info("RFProxy running.")
