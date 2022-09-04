import socket, json, time, sys, os

sys.path.append(os.path.dirname(os.path.abspath(os.path.dirname(__file__))))

import CSA
from BasicSA import getCommonValues, convertToRealDomain
from CommonValue import CSARound
from dto.CSASetupDto import CSASetupDto
import learning.federated_main as fl
import learning.models_helper as mhelper
import learning.utils as utils

class CSAServer:
    host = 'localhost'
    port = 8000
    SIZE = 2048
    ENCODING = 'utf-8'
    interval = 10       # server waits in one round # second
    timeout = 3
    totalRound = 4      # CSA has 4 rounds
    verifyRound = 'verify'
    isBasic = True      # true if BasicCSA, else FullCSA
    quantizationLevel = 30

    startTime = {}
    userNum = {}
    requests = {}
    run_data = []

    model = {}
    clusters = []       # list of cluster index
    users_keys = {}     # clients' public key
    R = 0

    survived = {}       # active user group per cluster
    S_list = {}
    IS = {}             # intermediate sum

    def __init__(self, n, k, isBasic, qLevel):
        self.n = n
        self.k = k # Repeat the entire process k times
        self.quantizationLevel = qLevel
        self.isBasic = isBasic
        self.serverSocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.serverSocket.bind((self.host, self.port))
        self.serverSocket.settimeout(self.timeout)

    def start(self):
        # start!
        self.serverSocket.listen()
        print(f'[{self.__class__.__name__}] Server started')

        requests = {round.name: {} for round in CSARound}
        self.requests_clusters = {round.name: {} for round in CSARound} # check completion of clusters
        requests[CSARound.SetUp.name][0] = []
        requests[self.verifyRound] = {} # for step 2: repete until all member share valid masks
        self.allTime = 0

        for j in range(self.k): # for k times
            # time
            self.start = time.time()
            self.setupTime = 0
            self.totalTime = 0

            # init
            self.users_keys = {}
            self.survived = {}
            self.S_list = {}
            self.IS = {}

            clientTag = CSARound.SetUp.name
            while True: # always listen
                currentClient = socket
                try:
                    clientSocket, addr = self.serverSocket.accept()
                    currentClient = clientSocket

                    # receive client data
                    # client request must ends with "\r\n"
                    request = ''
                    while True:
                        received = str(clientSocket.recv(self.SIZE), self.ENCODING)
                        if received.endswith("\r\n"):
                            received = received.replace("\r\n", "")
                            request = request + received
                            break
                        request = request + received

                    requestData = json.loads(request)
                    # request must contain {request: tag}
                    if requestData['request'] == 'table': # request data
                        clientSocket.sendall(bytes(json.dumps({'data': self.run_data}) + "\r\n", self.ENCODING))
                        continue
                    clientTag = requestData['request']
                    requestData.pop('request')
                    if clientTag == CSARound.SetUp.name:
                        cluster = 0
                    else:
                        cluster = int(requestData['cluster'])
                    requests[clientTag][cluster].append((clientSocket, requestData))

                    print(f'[{clientTag}] Client: {clientTag}/{addr}/{cluster}')

                except socket.timeout:
                    if clientTag == CSARound.SetUp.name: continue
                    now = time.time()
                    for c in self.clusters:
                        if (now - self.startTime[c]) >= self.perLatency[c]: # exceed
                            if clientTag == CSARound.ShareMasks.name:
                                if len(requests[self.verifyRound][c]) >= 1: # verify
                                    self.requests_clusters[CSARound.ShareMasks.name][c] = 0
                                elif self.requests_clusters[clientTag][c] == 1:
                                    self.saRound(clientTag, requests[clientTag][c], c)
                                    requests[clientTag][c] = [] # clear
                            elif clientTag != self.verifyRound and self.requests_clusters[clientTag][c] == 1:
                                self.saRound(clientTag, requests[clientTag][c], c)
                                requests[clientTag][c] = [] # clear
                    if sum(self.requests_clusters[CSARound.RemoveMasks.name].values()) == 0:
                        self.finalAggregation()
                        self.run_data.append([j+1, self.accuracy, self.setupTime, self.totalTime])
                        print("\n|---- {}: setupTime: {} totalTime: {} accuracy: {}%".format(j+1, self.setupTime, self.totalTime, self.accuracy))
                        break # end of this round
                    continue
                except:
                    print(f'[{self.__class__.__name__}] Exception: invalid request or unknown server error')
                    currentClient.sendall(bytes('Exception: invalid request or unknown server error\r\n', self.ENCODING))
                    pass

                if clientTag == CSARound.SetUp.name:
                    if len(requests[clientTag][0]) >= self.n:
                        self.setUp(requests[clientTag][0])
                        for round_t in CSARound:
                            for c in self.clusters:
                                requests[round_t.name][c] = []
                                self.requests_clusters[round_t.name][c] = 1
                                self.startTime[c] = time.time()
                        requests[self.verifyRound] = {c: [] for c in self.clusters}
                        requests[clientTag][0] = [] # clear
                else: # check all nodes in cluster sent request
                    for c in self.clusters:
                        nowN = len(self.survived[c])
                        if clientTag == CSARound.ShareMasks.name:
                            if clientTag == self.verifyRound:
                                if len(requests[self.verifyRound][c]) >= nowN: # verify ok
                                    self.requests_clusters[CSARound.ShareMasks.name][c] = 0
                            elif self.requests_clusters[clientTag][c] == 1 and len(requests[clientTag][c]) >= nowN:
                                self.saRound(clientTag, requests[clientTag][c], c)
                                requests[clientTag][c] = [] # clear
                        elif clientTag != self.verifyRound and self.requests_clusters[clientTag][c] == 1 and len(requests[clientTag][c]) >= nowN:
                            self.saRound(clientTag, requests[clientTag][c], c)
                            requests[clientTag][c] = [] # clear

                    if sum(self.requests_clusters[CSARound.RemoveMasks.name].values()) == 0:
                        self.finalAggregation()
                        self.run_data.append([j+1, self.accuracy, self.setupTime, self.totalTime])
                        print("\n|---- {}: setupTime: {} totalTime: {} accuracy: {}%".format(j+1, self.setupTime, self.totalTime, self.accuracy))
                        break # end of this round

        # End
        # serverSocket.close()
        print(f'[{self.__class__.__name__}] Server finished')
        print('\n|---- Total Time: ', self.allTime)

    def saRound(self, tag, requests, cluster):
        if tag == CSARound.ShareMasks.name:
            self.shareMasks(requests, cluster)
        elif tag == CSARound.Aggregation.name:
            self.aggregationInCluster(requests, cluster)
        elif tag == CSARound.RemoveMasks.name:
            self.removeDropoutMasks(requests, cluster)

    def setUp(self, requests):
        usersNow = len(requests)
        
        commonValues = getCommonValues()
        self.g = commonValues["g"]
        self.p = commonValues["p"]
        self.R = commonValues["R"]

        if self.model == {}:
            self.model = fl.setup()
        model_weights_list = mhelper.weights_to_dic_of_list(self.model.state_dict())
        user_groups = fl.get_user_dataset(usersNow)

        U = {}
        for i, request in enumerate(requests):
            #1. PS, GPS 받기
            socket, requestData = request
            gi = requestData['GPS_i']
            gj = requestData['GPS_j']
            PS = requestData['PS']
            U[i] = [gi, gj, PS, request]

        a = 6
        b = 8
        k = 3
        rf = 2
        cf = 1
        t = 4
        C = CSA.clustering(a, b, k, rf, cf, U, t)

        self.clusterNum = len(C)
        self.clusters = []     # list of clusters' index
        self.perLatency = {}   # define maximum latency(latency level) per cluster
        self.users_keys = {}   # {clusterIndex: {index: pk(:bytes)}}
        hex_keys = {}          # {clusterIndex: {index: pk(:hex)}}

        for i, cluster in C.items(): # get all users' public key
            self.clusters.append(i) # store clusters' index
            hex_keys[i] = {}
            self.users_keys[i] = {}
            self.perLatency[i] = 10 + i*5
            for j, request in cluster:
                hex_keys[i][j] = request[1]['pk']
                self.users_keys[i][j] = bytes.fromhex(hex_keys[i][j])

        list_ri, list_Ri = CSA.generateRandomNonce(self.clusters, self.g, self.p)

        for i, cluster in C.items():
            ri = list_ri[i]
            self.survived[i] = [j for j, request in cluster]
            clusterN = len(cluster)
            for j, request in cluster:
                response_ij = CSASetupDto(
                    n = usersNow, 
                    g = self.g, 
                    p = self.p, 
                    R = self.R, 
                    encrypted_ri = CSA.encrypt(self.users_keys[i][j], bytes(str(ri), 'ascii')).hex(),
                    Ri = list_Ri[i],
                    cluster = i,
                    clusterN = clusterN,
                    cluster_indexes = self.survived[i],
                    cluster_keys = str(hex_keys[i]), # node's id and public key
                    index = j,
                    qLevel = self.quantizationLevel,
                    data = [int(k) for k in user_groups[j]],
                    weights= str(model_weights_list),
                )._asdict()
                response_json = json.dumps(response_ij)
                clientSocket = request[0]
                clientSocket.sendall(bytes(response_json + "\r\n", self.ENCODING))

        self.setupTime = round(time.time() - self.start, 4)
    
    def shareMasks(self, requests, cluster):
        emask = {}
        pmask = {}
        self.survived[cluster] = []
        for (clientSocket, requestData) in requests:
            index = requestData['index']
            self.survived[cluster].append(int(index))
            pmask[index] = requestData['pmask']
            for k, mjk in requestData['emask'].items():
                k = int(k)
                if emask.get(k) is None:
                    emask[k] = {}
                emask[k][index] = mjk
        # print(self.survived[cluster])

        # send response
        for (clientSocket, requestData) in requests:
            index = requestData['index']
            response = {"emask": emask[index], "pmask": pmask}
            response_json = json.dumps(response)
            clientSocket.sendall(bytes(response_json + "\r\n", self.ENCODING))

        self.startTime[cluster] = time.time() # reset start time

    def aggregationInCluster(self, requests, cluster):
        beforeN = len(self.survived[cluster])
        self.survived[cluster] = []
        for (clientSocket, requestData) in requests:
            self.survived[cluster].append(int(requestData['index']))

        # print(self.survived[cluster])
        response = {'survived': self.survived[cluster]}
        response_json = json.dumps(response)

        self.S_list[cluster] = {}
        for (clientSocket, requestData) in requests:
            self.S_list[cluster][int(requestData['index'])] = requestData['S']
            clientSocket.sendall(bytes(response_json + "\r\n", self.ENCODING))
        
        if len(self.survived[cluster]) == beforeN and self.isBasic: # no need RemoveMasks stage in this cluster (step3-1 x)
            self.requests_clusters[CSARound.RemoveMasks.name][cluster] = 0
            self.IS[cluster] = list(sum(x) for x in zip(*self.S_list[cluster].values())) # sum

        self.requests_clusters[CSARound.Aggregation.name][cluster] = 0
        self.startTime[cluster] = time.time() # reset start time

    def removeDropoutMasks(self, requests, cluster):
        if len(requests) != len(self.survived[cluster]): # dropout happened in recovery phase
            self.survived[cluster] = []
            for (clientSocket, requestData) in requests:
                self.survived[cluster].append(int(requestData['index']))
            response = {'survived': self.survived[cluster]}
            response_json = bytes(json.dumps(response) + "\r\n", self.ENCODING)
            for (clientSocket, requestData) in requests:
                clientSocket.sendall(response_json)
        else:
            surv_S_list = []
            RS = 0
            a = 0
            response = {'survived': []} # means no need to send RS
            response_json = bytes(json.dumps(response) + "\r\n", self.ENCODING)
            for (clientSocket, requestData) in requests:
                surv_S_list.append(self.S_list[cluster][int(requestData['index'])])
                RS += int(requestData['RS'])
                if not self.isBasic:
                    a += int(requestData['a'])
                clientSocket.sendall(response_json)
            self.IS[cluster] = list(sum(x) + RS - a for x in zip(*surv_S_list))
            self.requests_clusters[CSARound.RemoveMasks.name][cluster] = 0

        self.startTime[cluster] = time.time() # reset start time
    
    def finalAggregation(self):
        sum_weights = list(sum(x) % self.p for x in zip(*self.IS.values())) # sum
        sum_weights = convertToRealDomain(sum_weights, self.quantizationLevel, self.p)

        # update global model
        new_weight = mhelper.restore_weights_tensor(mhelper.default_weights_info, sum_weights)
        final_userNum = sum(list(map(lambda c: len(self.survived[c]), self.survived.keys())))
        average_weight = utils.average_weight(new_weight, final_userNum)
        #print(average_weight['conv1.bias'])

        self.model.load_state_dict(average_weight)
        self.accuracy = round(fl.test_model(self.model), 4)

        self.totalTime = round(time.time() - self.start, 4)
        self.allTime = round(self.allTime + self.totalTime, 4)

    def close(self):
        self.serverSocket.close()


if __name__ == "__main__":
    server = CSAServer(n=4, k=3, isBasic = True, qLevel=30) # Basic CSA
    # server = CSAServer(n=4, k=1, isBasic = False, qLevel=30) # Full CSA
    server.start()