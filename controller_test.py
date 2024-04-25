from pox.core import core
import pox.openflow.libopenflow_01 as of
from pox.lib.revent import EventMixin
from pox.lib.util import dpidToStr
from pox.lib.addresses import EthAddr, IPAddr
from pox.openflow.discovery import Discovery
from collections import defaultdict
import heapq

log = core.getLogger()

class TelecomNetworkController(EventMixin):
    def __init__(self):
        self.listenTo(core.openflow)
        self.listenTo(core.openflow_discovery)
        self.flows = []
        self.adjacency = defaultdict(lambda: defaultdict(lambda: None))
        self.switches = {}
        self.mac_to_port = {}

    def _handle_ConnectionUp(self, event):
        dpid = dpidToStr(event.dpid)
        self.switches[dpid] = event.connection
        log.info("Switch %s has come up.", dpid)

    def _handle_LinkEvent(self, event):
        link = event.link
        dpid1 = dpidToStr(link.dpid1)
        dpid2 = dpidToStr(link.dpid2)
        port1 = link.port1
        port2 = link.port2
        if event.added:
            self.adjacency[dpid1][dpid2] = {'port': port1, 'weight': 1, 'bandwidth': 1000}
            self.adjacency[dpid2][dpid1] = {'port': port2, 'weight': 1, 'bandwidth': 1000}
            log.info("Link added: %s[%s] <-> %s[%s]", dpid1, port1, dpid2, port2)
        elif event.removed:
            if dpid2 in self.adjacency[dpid1]:
                del self.adjacency[dpid1][dpid2]
            if dpid1 in self.adjacency[dpid2]:
                del self.adjacency[dpid2][dpid1]
            log.info("Link removed: %s[%s] <-> %s[%s]", dpid1, port1, dpid2, port2)

    def _handle_PacketIn(self, event):
        packet = event.parsed
        in_port = event.port
        dpid = dpidToStr(event.connection.dpid)
        self.mac_to_port.setdefault(dpid, {})[packet.src] = in_port
        
        if packet.type == packet.IP_TYPE:
            ip_packet = packet.payload
            src_ip = str(ip_packet.srcip)
            dst_ip = str(ip_packet.dstip)
            log.info("Packet from %s to %s", src_ip, dst_ip)
            
            src_dpid, dst_dpid = self.get_switches_for_ips(src_ip, dst_ip)
            if src_dpid and dst_dpid:
                path = self.find_path(src_dpid, dst_dpid)
                if path:
                    self.install_path(event.connection, path, in_port, packet.dst, ip_packet)
                else:
                    log.warning("No path found from %s to %s", src_ip, dst_ip)

    def get_switches_for_ips(self, src_ip, dst_ip):
        src_mac = IPAddr(src_ip).toStr()
        dst_mac = IPAddr(dst_ip).toStr()

        src_dpid, dst_dpid = None, None
        
        for dpid, mac_port_map in self.mac_to_port.items():
            if src_mac in mac_port_map:
                src_dpid = dpid
            if dst_mac in mac_port_map:
                dst_dpid = dpid
            if src_dpid and dst_dpid:
                break

        return src_dpid, dst_dpid

    def add_flow(self, src, dst, start_time, duration, bandwidth):
        path = self.find_path(src, dst, bandwidth)
        if path:
            self.install_path(src, dst, path, bandwidth)
            flow = {'src': src, 'dst': dst, 'start_time': start_time, 'duration': duration, 'bandwidth': bandwidth}
            self.flows.append(flow)
            end_time = start_time + duration
            core.callDelayed(duration, self.remove_flow, flow)
            log.info("Flow added %s", flow)
        else:
            log.warning("No path found for flow from %s to %s with bandwidth %s", src, dst, bandwidth)

    def remove_flow(self, flow):
        if flow in self.flows:
            self.flows.remove(flow)
            log.info("Flow removed: %s", flow)
            path = self.find_path(flow['src'], flow['dst'], flow['bandwidth'])
            if path:
                for i in range(len(path) - 1):
                    u, v = path[i], path[i + 1]
                    self.adjacency[u][v]['bandwidth'] += flow['bandwidth']
                    self.adjacency[v][u]['bandwidth'] += flow['bandwidth']

    def find_path(self, src, dst, bandwidth):
        distances = {node: float('inf') for node in self.switches}
        previous_nodes = {node: None for node in self.switches}
        distances[src] = 0
        pq = [(0, src)]

        while pq:
            current_distance, current_node = heapq.heappop(pq)

            if current_node == dst:
                break

            if current_distance > distances[current_node]:
                continue

            for neighbor, props in self.adjacency[current_node].items():
                if props['bandwidth'] >= bandwidth:
                    distance = current_distance + props['weight']
                    if distance < distances[neighbor]:
                        distances[neighbor] = distance
                        previous_nodes[neighbor] = current_node
                        heapq.heappush(pq, (distance, neighbor))
                        

        path, node = [], dst
        while previous_nodes[node] is not None:
            path.insert(0, node)
            node = previous_nodes[node]
        if node == src:
            path.insert(0, src)
            return path
        return None

    def install_path(self, src, dst, path, bandwidth):
        for i in range(len(path) - 1):
            u, v = path[i], path[i + 1]
            msg = of.ofp_flow_mod()
            msg.match = of.ofp_match(dl_src=EthAddr(src), dl_dst=EthAddr(dst))
            msg.idle_timeout = 60
            msg.hard_timeout = 300
            msg.actions.append(of.ofp_action_output(port=self.adjacency[u][v]['port']))
            self.switches[u].send(msg)

            self.adjacency[u][v]['bandwidth'] -= bandwidth
            self.adjacency[v][u]['bandwidth'] -= bandwidth

def launch():
    core.registerNew(TelecomNetworkController)
    core.register("openflow_discovery", Discovery())

if __name__ == "__main__":
    launch()
