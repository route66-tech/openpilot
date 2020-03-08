#!/usr/bin/env python3
#import sys
# sys.path.append("/root/arne-openpilot/openpilot")
# setup logging
import logging
import logging.handlers


# Add phonelibs openblas to LD_LIBRARY_PATH if import fails
from scipy import spatial

import selfdrive.crash as crash


#DEFAULT_SPEEDS_BY_REGION_JSON_FILE = BASEDIR + "/selfdrive/mapd/default_speeds_by_region.json"
#from selfdrive.mapd import default_speeds_generator
#default_speeds_generator.main(DEFAULT_SPEEDS_BY_REGION_JSON_FILE)

import time
import requests
import threading
import numpy as np
import overpy
from common.params import Params
from collections import defaultdict
from selfdrive.version import version, dirty

from common.transformations.coordinates import geodetic2ecef
import cereal.messaging_arne as messaging_arne
import cereal.messaging as messaging

from selfdrive.mapd.mapd_helpers import MAPS_LOOKAHEAD_DISTANCE, Way, circle_through_points, rate_curvature_points
#from copy import deepcopy



# define LoggerThread class to implement logging functionality
class LoggerThread(threading.Thread):
    def __init__(self, threadID, name):
        threading.Thread.__init__(self)
        self.threadID = threadID
        self.name = name
        self.logger = logging.getLogger(name)
        h = logging.handlers.RotatingFileHandler(str(name)+'-Thread.log', 'a', 10*1024*1024, 5) 
        f = logging.Formatter('%(asctime)s %(processName)-10s %(name)s %(levelname)-8s %(message)s')
        h.setFormatter(f)
        self.logger.addHandler(h)
        self.logger.setLevel(logging.CRITICAL) # set to logging.DEBUG to enable logging
        # self.logger.setLevel(logging.DEBUG) # set to logging.DEBUG to enable logging
        
    def save_gps_data(self, gps):
        try:
            location = [gps.speed, gps.bearing, gps.latitude, gps.longitude, gps.altitude, gps.accuracy, time.time()]
            with open("/data/openpilot/selfdrive/data_collection/gps-data", "a") as f:
                f.write("{}\n".format(location))
        except:
            self.logger.error("Unable to write gps data to external file")
            
    def run(self):
        pass # will be overridden in the child class

class QueryThread(LoggerThread):
    def __init__(self, threadID, name, sharedParams={}): # sharedParams is dict of params shared between two threads
        # invoke parent constructor https://stackoverflow.com/questions/2399307/how-to-invoke-the-super-constructor-in-python
        LoggerThread.__init__(self, threadID, name)
        self.sharedParams = sharedParams
        # memorize some parameters
        self.OVERPASS_API_URL = "https://z.overpass-api.de/api/interpreter"
        self.OVERPASS_API_URL2 = "https://lz4.overpass-api.de/api/interpreter"
        self.OVERPASS_HEADERS = {
            'User-Agent': 'NEOS (comma.ai)',
            'Accept-Encoding': 'gzip'
        }
        self.prev_ecef = None

    def is_connected_to_internet(self, timeout=5):
        try:
            requests.get(self.OVERPASS_API_URL, timeout=timeout)
            self.logger.debug("connection 1 active")
            return True
        except:
            self.logger.error("No internet connection available.")
            return False 

    def is_connected_to_internet2(self, timeout=5):
        try:
            requests.get(self.OVERPASS_API_URL2, timeout=timeout)
            self.logger.debug("connection 2 active")
            return True
        except:
            self.logger.error("No internet connection available.")
            return False 

    def build_way_query(self, lat, lon, radius=50):
        """Builds a query to find all highways within a given radius around a point"""
        pos = "  (around:%f,%f,%f)" % (radius, lat, lon)
        lat_lon = "(%f,%f)" % (lat, lon)
        q = """(
        way
        """ + pos + """
        [highway][highway!~"^(footway|path|bridleway|steps|cycleway|construction|bus_guideway|escape)$"];
        >;);out;""" + """is_in""" + lat_lon + """;area._[admin_level~"[24]"];
        convert area ::id = id(), admin_level = t['admin_level'],
        name = t['name'], "ISO3166-1:alpha2" = t['ISO3166-1:alpha2'];out;
        """
        self.logger.debug("build_way_query : %s" % str(q))
        return q


    def run(self):
        self.logger.debug("run method started for thread %s" % self.name)
        api = overpy.Overpass(url=self.OVERPASS_API_URL)
        # for now we follow old logic, will be optimized later
        while True:
            time.sleep(1)
            self.logger.debug("Starting after sleeping for 1 second ...")
            last_gps = self.sharedParams.get('last_gps', None)
            self.logger.debug("last_gps = %s" % str(last_gps))
            if last_gps is not None:
                fix_ok = last_gps.flags & 1
                if not fix_ok:
                    continue

            last_query_pos = self.sharedParams.get('last_query_pos', None)
            if last_query_pos is not None:
                cur_ecef = geodetic2ecef((last_gps.latitude, last_gps.longitude, last_gps.altitude))
                if self.prev_ecef is None:
                    self.prev_ecef = geodetic2ecef((last_query_pos.latitude, last_query_pos.longitude, last_query_pos.altitude))
                # for next step cur_ecef becomes prev_ecef
                
                dist = np.linalg.norm(cur_ecef - self.prev_ecef)
                if dist < 3000: #updated when we are 1km from the edge of the downloaded circle
                    continue
                    # self.logger.debug("parameters, cur_ecef = %s, prev_ecef = %s, dist=%s" % (str(cur_ecef), str(prev_ecef), str(dist)))

                if dist > 4000:
                    query_lock = self.sharedParams.get('query_lock', None)
                    if query_lock is not None:
                        query_lock.acquire()
                        self.sharedParams['cache_valid'] = False
                        query_lock.release()
                    else:
                        self.logger.error("There is no query_lock")

            if last_gps is not None and (self.is_connected_to_internet() or self.is_connected_to_internet2()):
                q = self.build_way_query(last_gps.latitude, last_gps.longitude, radius=4000)
                try:
                    try:
                        new_result = api.query(q)
                        self.logger.debug("new_result = %s" % str(new_result))
                    except:
                        api2 = overpy.Overpass(url=self.OVERPASS_API_URL2)
                        self.logger.error("Using backup Server")
                        new_result = api2.query(q)

                    # Build kd-tree
                    nodes = []
                    real_nodes = []
                    node_to_way = defaultdict(list)
                    location_info = {}

                    for n in new_result.nodes:
                        nodes.append((float(n.lat), float(n.lon), 0))
                        real_nodes.append(n)

                    for way in new_result.ways:
                        for n in way.nodes:
                            node_to_way[n.id].append(way)

                    for area in new_result.areas:
                        if area.tags.get('admin_level', '') == "2":
                            location_info['country'] = area.tags.get('ISO3166-1:alpha2', '')
                        elif area.tags.get('admin_level', '') == "4":
                            location_info['region'] = area.tags.get('name', '')

                    nodes = np.asarray(nodes)
                    nodes = geodetic2ecef(nodes)
                    tree = spatial.cKDTree(nodes)
                    self.logger.debug("query thread, ... %s %s" % (str(nodes), str(tree)))

                    # write result
                    query_lock = self.sharedParams.get('query_lock', None)
                    if query_lock is not None:
                        query_lock.acquire()
                        self.sharedParams['last_query_result'] = new_result, tree, real_nodes, node_to_way, location_info
                        self.prev_ecef = geodetic2ecef((last_gps.latitude, last_gps.longitude, last_gps.altitude))
                        self.sharedParams['last_query_pos'] = last_gps
                        self.sharedParams['cache_valid'] = True
                        query_lock.release()
                    else:
                        self.logger.error("There is not query_lock")

                except Exception as e:
                    self.logger.error("ERROR :" + str(e))
                    crash.capture_warning(e)
                    query_lock = self.sharedParams.get('query_lock', None)
                    query_lock.acquire()
                    self.sharedParams['last_query_result'] = None
                    query_lock.release()
            else:
                query_lock = self.sharedParams.get('query_lock', None)
                query_lock.acquire()
                self.sharedParams['last_query_result'] = None
                query_lock.release()

            self.logger.debug("end of one cycle in endless loop ...")

class MapsdThread(LoggerThread):
    def __init__(self, threadID, name, sharedParams={}):
        # invoke parent constructor 
        LoggerThread.__init__(self, threadID, name)
        self.sharedParams = sharedParams
        self.sm = messaging.SubMaster(['gpsLocationExternal'])
        self.arne_sm = messaging_arne.SubMaster(['liveTrafficData'])
        self.pm = messaging.PubMaster(['liveMapData'])
        self.logger.debug("entered mapsd_thread, ... %s, %s, %s" % (str(self.sm), str(self.arne_sm), str(self.pm)))
    def run(self):
        self.logger.debug("Entered run method for thread :" + str(self.name))
        cur_way = None
        curvature_valid = False
        curvature = None
        upcoming_curvature = 0.
        dist_to_turn = 0.
        road_points = None
        speedLimittraffic = 0
        speedLimittraffic_prev = 0
        max_speed = None
        max_speed_ahead = None
        max_speed_ahead_dist = None

        max_speed_prev = 0
        speedLimittrafficvalid = False

        while True:
            time.sleep(0.1)
            self.logger.debug("starting new cycle in endless loop")
            self.sm.update(0)
            self.arne_sm.update(0)
            gps_ext = self.sm['gpsLocationExternal']
            traffic = self.arne_sm['liveTrafficData']
            # if True:  # todo: should this be `if sm.updated['liveTrafficData']:`?
            # commented out the previous line because it does not make sense

            self.logger.debug("got gps_ext = %s" % str(gps_ext))
            if traffic.speedLimitValid:
                speedLimittraffic = traffic.speedLimit
                if abs(speedLimittraffic_prev - speedLimittraffic) > 0.1:
                    speedLimittrafficvalid = True
                    speedLimittraffic_prev = speedLimittraffic
            else:
                speedLimittrafficvalid = False
            if traffic.speedAdvisoryValid:
                speedLimittrafficAdvisory = traffic.speedAdvisory
                speedLimittrafficAdvisoryvalid = True
            else:
                speedLimittrafficAdvisoryvalid = False
            # commented out because it is never going to happen
            # else:
                # speedLimittrafficAdvisoryvalid = False

            if self.sm.updated['gpsLocationExternal']:
                gps = gps_ext
                self.save_gps_data(gps)
            else:
                continue
            query_lock = self.sharedParams.get('query_lock', None)
            # last_gps = self.sharedParams.get('last_gps', None)
            query_lock.acquire()
            self.sharedParams['last_gps'] = gps
            query_lock.release()
            self.logger.debug("setting last_gps to %s" % str(gps))

            fix_ok = gps.flags & 1
            self.logger.debug("fix_ok = %s" % str(fix_ok))

            if gps.accuracy > 2.0 and not speedLimittrafficvalid:
                fix_ok = False
            if not fix_ok or self.sharedParams['last_query_result'] is None or not self.sharedParams['cache_valid']:
                self.logger.debug("fix_ok %s" % fix_ok)
                self.logger.error("Error in fix_ok logic")
                cur_way = None
                curvature = None
                max_speed_ahead = None
                max_speed_ahead_dist = None
                curvature_valid = False
                upcoming_curvature = 0.
                dist_to_turn = 0.
                road_points = None
                map_valid = False
            else:
                map_valid = True
                lat = gps.latitude
                lon = gps.longitude
                heading = gps.bearing
                speed = gps.speed

                query_lock.acquire()
                # making a copy of sharedParams so I do not have to pass the original to the Way.closest method
                #last_q_result = deepcopy(self.sharedParams.get('last_query_result', None))
                cur_way = Way.closest(self.sharedParams['last_query_result'], lat, lon, heading, cur_way)
                query_lock.release()

                if cur_way is not None:
                    self.logger.debug("cur_way is not None ...")
                    pnts, curvature_valid = cur_way.get_lookahead(lat, lon, heading, MAPS_LOOKAHEAD_DISTANCE)
                    if pnts is not None:
                        xs = pnts[:, 0]
                        ys = pnts[:, 1]
                        road_points = [float(x) for x in xs], [float(y) for y in ys]

                        if speed < 5:
                            curvature_valid = False
                        if curvature_valid and pnts.shape[0] <= 3:
                            curvature_valid = False
                    else:
                        curvature_valid = False
                        upcoming_curvature = 0.
                        curvature = None
                        dist_to_turn = 0.
                    # The curvature is valid when at least MAPS_LOOKAHEAD_DISTANCE of road is found
                    if curvature_valid:
                    # Compute the curvature for each point
                        with np.errstate(divide='ignore'):
                            circles = [circle_through_points(*p, direction=True) for p in zip(pnts, pnts[1:], pnts[2:])]
                            circles = np.asarray(circles)
                            radii = np.nan_to_num(circles[:, 2])
                            radii[abs(radii) < 15.] = 10000

                            if cur_way.way.tags['highway'] == 'trunk':
                                radii = radii*1.6 # https://media.springernature.com/lw785/springer-static/image/chp%3A10.1007%2F978-3-658-01689-0_21/MediaObjects/298553_35_De_21_Fig65_HTML.gif
                            elif cur_way.way.tags['highway'] == 'motorway' or  cur_way.way.tags['highway'] == 'motorway_link':
                                radii = radii*2.8

                            curvature = 1. / radii
                        rate = [rate_curvature_points(*p) for p in zip(pnts[1:], pnts[2:],curvature[0:],curvature[1:])]
                        rate = ([0] + rate)

                        curvature = np.abs(curvature)
                        curvature = np.multiply(np.minimum(np.multiply(rate,4000)+0.7,1.1),curvature)
                        # Index of closest point
                        closest = np.argmin(np.linalg.norm(pnts, axis=1))
                        dist_to_closest = pnts[closest, 0]  # We can use x distance here since it should be close

                        # Compute distance along path
                        dists = list()
                        dists.append(0)
                        for p, p_prev in zip(pnts, pnts[1:, :]):
                            dists.append(dists[-1] + np.linalg.norm(p - p_prev))
                        dists = np.asarray(dists)
                        dists = dists - dists[closest] + dist_to_closest
                        dists = dists[1:-1]

                        close_idx = np.logical_and(dists > 0, dists < 500)
                        dists = dists[close_idx]
                        curvature = curvature[close_idx]

                        if len(curvature):
                            curvature = np.nan_to_num(curvature)


                            upcoming_curvature = np.amax(curvature)
                            dist_to_turn =np.amin(dists[np.logical_and(curvature >= upcoming_curvature, curvature <= upcoming_curvature)])


                        else:
                            upcoming_curvature = 0.
                            dist_to_turn = 999

                #query_lock.release()

            dat = messaging.new_message()
            dat.init('liveMapData')

            last_gps = self.sharedParams.get('last_gps', None)

            if last_gps is not None:  # TODO: this should never be None with SubMaster now
                dat.liveMapData.lastGps = last_gps

            if cur_way is not None:
                dat.liveMapData.wayId = cur_way.id

                # Speed limit
                max_speed = cur_way.max_speed(heading)
                max_speed_ahead = None
                max_speed_ahead_dist = None
                if max_speed is not None:
                    max_speed_ahead, max_speed_ahead_dist = cur_way.max_speed_ahead(max_speed, lat, lon, heading, MAPS_LOOKAHEAD_DISTANCE)
                else:
                    max_speed_ahead, max_speed_ahead_dist = cur_way.max_speed_ahead(speed*1.1, lat, lon, heading, MAPS_LOOKAHEAD_DISTANCE)
                    # TODO: anticipate T junctions and right and left hand turns based on indicator

                if max_speed_ahead is not None and max_speed_ahead_dist is not None:
                    dat.liveMapData.speedLimitAheadValid = True
                    dat.liveMapData.speedLimitAhead = float(max_speed_ahead)
                    dat.liveMapData.speedLimitAheadDistance = float(max_speed_ahead_dist)
                if max_speed is not None:
                    if abs(max_speed - max_speed_prev) > 0.1:
                        speedLimittrafficvalid = False
                        max_speed_prev = max_speed
                advisory_max_speed = cur_way.advisory_max_speed()
                if speedLimittrafficAdvisoryvalid:
                    dat.liveMapData.speedAdvisoryValid = True
                    dat.liveMapData.speedAdvisory = speedLimittrafficAdvisory / 3.6
                else:
                    if advisory_max_speed is not None:
                        dat.liveMapData.speedAdvisoryValid = True
                        dat.liveMapData.speedAdvisory = advisory_max_speed

                # Curvature
                dat.liveMapData.curvatureValid = curvature_valid
                dat.liveMapData.curvature = float(upcoming_curvature)
                dat.liveMapData.distToTurn = float(dist_to_turn)
                if road_points is not None:
                    dat.liveMapData.roadX, dat.liveMapData.roadY = road_points
                if curvature is not None:
                    dat.liveMapData.roadCurvatureX = [float(x) for x in dists]
                    dat.liveMapData.roadCurvature = [float(x) for x in curvature]

            if speedLimittrafficvalid:
                if speedLimittraffic > 0.1:
                    dat.liveMapData.speedLimitValid = True
                    dat.liveMapData.speedLimit = speedLimittraffic / 3.6
                    map_valid = False
                else:
                    speedLimittrafficvalid = False
            else:
                if max_speed is not None and map_valid:
                    dat.liveMapData.speedLimitValid = True
                    dat.liveMapData.speedLimit = max_speed

            dat.liveMapData.mapValid = map_valid
            #
            self.logger.debug("Sending ... liveMapData ... %s", str(dat))
            self.pm.send('liveMapData', dat)

def main():
    params = Params()
    dongle_id = params.get("DongleId")
    crash.bind_user(id=dongle_id)
    crash.bind_extra(version=version, dirty=dirty, is_eon=True)
    crash.install()
    # initialize gps parameters
    # initialize last_gps
    # current_milli_time = lambda: int(round(time.time() * 1000))


    # setup shared parameters
    last_gps = None
    query_lock = threading.Lock()
    last_query_result = None
    last_query_pos = None
    cache_valid = False

    sharedParams = {'last_gps' : last_gps, 'query_lock' : query_lock, 'last_query_result' : last_query_result, \
            'last_query_pos' : last_query_pos, 'cache_valid' : cache_valid}

    qt = QueryThread(1, "QueryThread", sharedParams=sharedParams)
    mt = MapsdThread(2, "MapsdThread", sharedParams=sharedParams)

    qt.start()
    mt.start()
    # print("Threads started")
    # qt.run()
    # qt.join()



if __name__ == "__main__":
    main()
