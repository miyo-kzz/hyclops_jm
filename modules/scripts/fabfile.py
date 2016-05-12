#!/usr/bin/env python
#-*- coding: utf-8 -*

"""
ZabbixとJobSchedulerの連携プログラム
  １．$JM_HOME/live配下にJobSchedulerに登録したJob情報を置くと
　　１）JobSchedulerにJobを登録する。
    ２）ZabbixにJobとJob Chainの実行時間を監視するアイテムと実行結果を
        監視するアイテムとその実行結果からアラートを上げるトリガを登録
        する。
  ２．外部スクリプトを実行したときに、上記のアイテムにzabbix_senderで
      登録する。
  ３．ジョブが実行したときにトリガの設定を一時的に変更する。
         （注意）テンプレートを使用して明示的に実行する必要があります。

"""

__author__  = "TIS inc."
__version__ = "1.00"
__date__    = "2014/xx/xx"


#############################################################
# インポートモジュール
#############################################################

#============================================================
# For Base
from fabric.api import lcd, cd, local, env, hide
import sys, os, os.path, time, fnmatch
from datetime import datetime as dt
from datetime import datetime

#============================================================
# For soap
import httplib

#============================================================
# For socket
import socket
from contextlib import closing

#============================================================
# For xml
from xml.dom import minidom
from StringIO import StringIO
import xml.etree.ElementTree as ET
from xml.etree import ElementTree

#============================================================
# For json
import json

#============================================================
# For PostgreSQL
import psycopg2


#############################################################
# システム情報
#############################################################
# 値はデフォルト値　実際に使うのはDB情報
env.jos_server="localhost"
env.jos_port=4444
env.zbx_server="localhost"
env.zbx_login="Admin"
env.zbx_pass="zabbix"

#============================================================
# DBへの接続情報
env.psqldatabase='hyclops_jm'
env.psqluser='HYCLOPS_JM_USER'
env.psqlpassword='HYCLOPS_JM_USER'
env.psqlhost='127.0.0.1'
env.psqlport=5432

env.jos_timeout=5
env.dbg=1

#############################################################
# グローバルバッファ
#############################################################

env.job_list={}
env.job_dirs={}
env.jos_server_list={}
env.jos_job=[]
env.jos_job_chain=[]
env.jos_order={}
env.process_class={}
env.zbx_server_list={}
env.zbx_id=100
env.inited=0
env.jos_last_id={}


#############################################################
# ライブラリ モジュール
#############################################################

#============================================================
def help():
	print "hyclops_jm <コマンド>[:パラメータ[,パラメータ].....]"
	print ""
	print "[コマンド]"
	print "	show_info	： JobSchedulerからジョブ情報を取得してzabbix_senderで"
	print "				Zabbixにジョブの処理時間を送信する"
	print "	set_job_items	： Zabbixにジョブのitemを設定する"
	print "	set_jobs    	： Level Discavery用にジョブ情報をJSONでZabbixに送信する"
	print "	trigger_switch	： 現状のTriggerを無効にして代わりを設定する"
	print "		[パラメータ]	： hostid	： 無効にするホストID"
	print "				　 msg		： 無効にするトリガ名"
	print "				　 rule		： 無効後に登録するトリガ条件"
	print "	trigger_ret	： trigger_switchで設定した内容を元に戻す"
	print "		[パラメータ]	： hostid	： 有効にするホストID"
	print "				source_triggerid		: 有効にするトリガID"
	print "				dest_triggerid		: 削除するトリガID"


#############################################################
# ライブラリ モジュール
#############################################################

#============================================================
def getdbinfo(dbg=0):
	"""
	getdbinfo
	PostgreSQLからシステム情報を取得
	@param		： None

	@reruen		： None
	"""

	if env.inited == 1:				# 複数回呼ばれても１回しか実行しない
		return

	connection = psycopg2.connect(
	 database = env.psqldatabase,
	 user = env.psqluser,
	 password = env.psqlpassword,
	 host = env.psqlhost,
	 port = env.psqlport)

	cur = connection.cursor()			# DBへの接続

	cur.execute("SELECT * FROM sysinfo")		# システム情報の取得

	for row in cur:
		if dbg == "1":
			print("%s : %s" % (row[0], row[1]))

		if row[0] == 'jos_server':
			env.jos_server=row[1]
		elif row[0] == 'jos_port':
			env.jos_port=int(row[1],10)
		elif row[0] == 'zbx_server':
			env.zbx_server=row[1]
		elif row[0] == 'zbx_login':
			env.zbx_login=row[1]
		elif row[0] == 'zbx_pass':
			env.zbx_pass=row[1]
		elif row[0] == 'jos_timeout':
			env.zbx_timeout=int(row[1],10)

	cur.execute("SELECT * FROM jobid_tbl")		# 前回実行時のTask IDの取得

	for row in cur:
		if dbg == "1":
			print("%s : %s" % (row[0], row[1]))

		env.jos_last_id[row[0]] = row[1]


	cur.close()					# DBへの切断
	connection.close()

	env.inited = 1					# 複数回呼び出しの対応

#============================================================
def getzbx(SoapMessage,dbg=0):
	"""
	getzbx
	Zabbix APIにJSON-RPCでアクセスする
	@param	SoapMessage	： Zabbixへ送信するJSON-RPCコマンド

	@reruen	res		： Zabbixからの返信
	"""

	getdbinfo(dbg)

	#construct and send the header

	webservice = httplib.HTTP("%s" % (env.zbx_server) )
	webservice.putrequest("POST", "/zabbix/api_jsonrpc.php")
	webservice.putheader("Host", "172.0.0.1")
	webservice.putheader("User-Agent", "Python post")
	webservice.putheader("Content-type", "application/json-rpc")
	webservice.putheader("Content-length", "%d" % len(SoapMessage))
	webservice.endheaders()
	webservice.send(SoapMessage)

	# get the response

	statuscode, statusmessage, header = webservice.getreply()
	res = webservice.getfile().read()

	return res

#============================================================
def getzbx_login(id,dbg=0):
	"""
	getzbx_login
	Zabbixから認証情報を取得する
	@param	id		： id情報

	@reruen	auth_data	： Zabbixから取得した認証情報
	"""

	getdbinfo(dbg)

	# a "as lighter as possible" soap message:

	SM_TEMPLATE_AUTH = """{"auth":null,"method":"user.login","id":%s,"params":
	{"user":"%s","password":"%s"},"jsonrpc":"2.0"}"""

	SoapMessage = SM_TEMPLATE_AUTH % (id,env.zbx_login,env.zbx_pass)
	if dbg == "1":
		print SoapMessage
	res = getzbx( SoapMessage,dbg )

	if dbg == "1":
		print res

	recvbuf = json.loads(res)

	if recvbuf.has_key('error'):
		print 'Error occurred.'
		print recvbuf
		return

	auth_data = recvbuf['result']

	return auth_data

#============================================================
def jos_soap(SoapMessage,dbg=0):
	"""
	jos_soap
	JobSchedulerにsoapでアクセスする
	@param	SoapMessage	： JobSchedulerへsoapで送信する

	@reruen	auth_data	： JobSchedulerから取得した情報
	"""

	getdbinfo(dbg)

	#construct and send the header

	webservice = httplib.HTTP("%s:%s" % (env.jos_server,env.jos_port) )
	webservice.putrequest("POST", "/scheduler")
	webservice.putheader("Host", "%s") % env.jos_server
	webservice.putheader("User-Agent", "Python post")
	webservice.putheader("Content-type", "application/soap+xml;charset=UTF-8")
	webservice.putheader("Content-length", "%d" % len(SoapMessage))
	webservice.putheader("SOAPAction", "\"\"")
	webservice.endheaders()
	webservice.send(SoapMessage)

	# get the response

	statuscode, statusmessage, header = webservice.getreply()
	print "Response: ", statuscode, statusmessage
	print "headers: ", header
	res = webservice.getfile().read()

	if dbg == '1':
		print res

	return res

#============================================================
def jos_xml(XmlMessage,dbg=0):
	"""
	jos_xml
	JobSchedulerにXMLコマンドを送信する
	@param	SoapMessage	： JobSchedulerへ送信するXMLコマンド

	@reruen	auth_data	： JobSchedulerから取得した情報
	"""

	dbg = env.dbg
	getdbinfo(dbg)

	bufsize = 524288

	if dbg == "1":
		print 'jos_xml Command : ',XmlMessage

	recvbuf = ''
	sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
	sock.settimeout(env.jos_timeout)
	with closing(sock):
		sock.connect((env.jos_server, env.jos_port))
		sock.send(b'%s' % XmlMessage)
		while 1:
			try:
				tmp = sock.recv(bufsize)
			except socket.timeout:
				if dbg == "1":
					print 'End Timeout'
				break

			if not tmp:
				if dbg == "1":
					print 'End'
				break

			recvbuf = recvbuf + tmp

	if dbg == "1":
		print "===< jos_xml >==="
		print recvbuf
		print "===< jos_xml >==="

	while recvbuf[-1:] <> '>':				# 最後の文字が">"になるまで削除
		tmpbuf = recvbuf[:-1]
		recvbuf = tmpbuf

	tmpbuf = recvbuf.replace('&', '#amp')			# ＆がエラーとなるための回避
	recvbuf = tmpbuf

	return recvbuf

#============================================================
# XMLフォーマットの情報をすべて表示する

def printAllElement(node, hierarchy=1):
	"""
	printAllElement
	XMLフォーマットの情報をすべて表示する
	@param	node	： ID情報
	@param	hierarcy： 表示するときのTABの数

	@reruen		： None
	"""

	# スペース調整
	space = ''
	for i in range(hierarchy*4):
		space += ' '

	# エレメントノードの場合はタグ名を表示する
	if node.nodeType == node.ELEMENT_NODE:
		print("{0}{1}".format(space, node.tagName))
		if node.attributes.keys():
			for attr in node.attributes.keys():
				print("ATTR{0}  --{1} : {2}".\
					format(space,node.attributes[attr].name,\
					node.attributes[attr].value))
		# 再帰呼び出し
		for child in node.childNodes:
			printAllElement(child, hierarchy+1)

	# テキストもしくはコメントだった場合dataを表示する
	elif node.nodeType in [node.TEXT_NODE, node.COMMENT_NODE]:
	# スペースを取り除く
		data = node.data.replace(' ', '')

		# 改行のみではなかった時のみ表示する
		if data!='\n':
			print("PARA{0}<{1}>".format(space, node.data))



#############################################################
# サブルーティン関数
#############################################################

#============================================================

def zbx_getitems(hostid="10084",dbg=0):
	"""
	zbx_getitems
	Zabbixから指定したホストのitem情報を得る
	@param	hostid		： 取得するアイテムのhostid

	@reruen	recvbuf		： Zabbixから取得した情報
	"""

	getdbinfo(dbg)

	id = env.zbx_id

	# a "as lighter as possible" soap message:

	SM_TEMPLATE_ITEM_GET = """{ "jsonrpc": "2.0", "method": "item.get", "params": {
        "output": "extend", "hostids": "%s",
        "sortfield": "name" }, "auth":"%s", "id": %s } """

	auth_data = getzbx_login(id)

	SoapMessage = SM_TEMPLATE_ITEM_GET % (hostid, auth_data, id)
	res = getzbx( SoapMessage )
	recvbuf = json.loads(res)

	if recvbuf.has_key('error'):
		print 'Error occurred.'
		print recvbuf
		return

	if dbg == '1':
		print json.dumps(recvbuf, indent=4)			# print All elements of JSON

	return recvbuf


#============================================================

def zbx_item_exist(keys,hostid,dbg=0):
	"""
	zbx_item_exist
	Zabbixに指定したホストに指定したitemが有るかを確認する
	@param	keys		： 確認するアイテムのKay
	@param	hostid		： 確認するアイテムのhostid

	@reruen	recvbuf		： Zabbixから取得した情報
	"""

	getdbinfo(dbg)

	id = env.zbx_id

	# a "as lighter as possible" soap message:

	SM_TEMPLATE_ITEM_EXIST = """ { "jsonrpc": "2.0", "method": "item.exists", "params": {
	"hostid": "%s", "key_": "%s"
	}, "auth": "%s", "id": %s } """

	auth_data = getzbx_login(id)

	SoapMessage = SM_TEMPLATE_ITEM_EXIST % (keys,hostid, auth_data, id)
	res = getzbx( SoapMessage )
	recvbuf = json.loads(res)

	if recvbuf.has_key('error'):
		print 'Error occurred.'
		print recvbuf
		return

	if dbg == '1':
		print json.dumps(recvbuf, indent=4)			# print All elements of JSON

	return recvbuf


#============================================================

def zbx_setitems(name,hostid="10084",dbg=0):
	"""
	zbx_setitems
	Zabbixに指定したホストにitemを設定する
	@param	name		： 確認するアイテムのKay（アイテム名）
	@param	hostid		： 確認するアイテムのhostid

	@reruen	recvbuf		： Zabbixから取得した情報
	"""

	getdbinfo(dbg)

	id = env.zbx_id

	# a "as lighter as possible" soap message:

	SM_TEMPLATE_ITEM_SET = """ { "jsonrpc": "2.0", "method": "item.create", "params": {
        "name": "Jobscheduler's Job (%s)", "key_": "%s", "hostid": "%s", "type": 2,
        "value_type": 3, "interfaceid": "0", "delay": 30
	}, "auth": "%s", "id": %s } """

	auth_data = getzbx_login(id)

	SoapMessage = SM_TEMPLATE_ITEM_SET % (name,name,hostid, auth_data, id)
	res = getzbx( SoapMessage )
	recvbuf = json.loads(res)

	if recvbuf.has_key('error'):
		print 'Error occurred.'
		print recvbuf
		return

	if dbg == '1':
		print json.dumps(recvbuf, indent=4)			# print All elements of JSON

	return recvbuf


#============================================================

def zbx_delitems(itemid,dbg=0):
	"""
	zbx_delitems
	Zabbixから指定したitemを削除する
	@param	hostid		： 削除するアイテムのhostid

	@reruen	recvbuf		： Zabbixから取得した情報
	"""

	getdbinfo(dbg)

	id = env.zbx_id

	# a "as lighter as possible" soap message:

	SM_TEMPLATE_ITEM_SET = """ { "jsonrpc": "2.0", "method": "item.delete", "params": [
	"%s" ], "auth": "%s", "id": %s } """

	auth_data = getzbx_login(id)

	SoapMessage = SM_TEMPLATE_ITEM_SET % (itemid, auth_data, id)
	res = getzbx( SoapMessage )
	recvbuf = json.loads(res)

	if recvbuf.has_key('error'):
		print 'Error occurred.'
		print recvbuf
		return

	if dbg == '1':
		print json.dumps(recvbuf, indent=4)			# print All elements of JSON

	return recvbuf


#============================================================

def zbx_gettrigger(hostid="10084",dbg=0):
	"""
	zbx_gettrigger
	Zabbixから指定したホストのtrigger情報を取得する
	@param	hostid		： 取得するするトリガのhostid

	@reruen	recvbuf		： Zabbixから取得した情報
	"""

	getdbinfo(dbg)

	id = env.zbx_id

	# a "as lighter as possible" soap message:

	SM_TEMPLATE_ITEM_GET2 = """{ "jsonrpc": "2.0", "method": "trigger.get", "params": {
	"output": "extend", "hostids": "%s", "selectFunctions": "extend"
	}, "auth": "%s", "id": %s } """

	SM_TEMPLATE_ITEM_GET = """{ "jsonrpc": "2.0", "method": "trigger.get", "params": {
	"output": "extend",
	"expandExpression": "True",
	"hostids": "%s"
	}, "auth": "%s", "id": %s } """

	auth_data = getzbx_login(id)

	SoapMessage = SM_TEMPLATE_ITEM_GET % (hostid, auth_data, id)
	if dbg in ["1","2"]:
		print SoapMessage
	res = getzbx( SoapMessage )
	recvbuf = json.loads(res)

	if recvbuf.has_key('error'):
		print 'Error occurred.'
		print recvbuf
		return

	if dbg == '1':
		print json.dumps(recvbuf, indent=4)			# print All elements of JSON

	return recvbuf


#============================================================

def zbx_set_trigger(hostid, exp, desp, pri=3, dbg=0):
	"""
	zbx_set trigger
	Zabbixに指定したホストに指定したtriggerを設定する
	@param	hostid		： 設定するするトリガのhostid
	@param	exp		： 設定するするトリガ名
	@param	desp		： 設定するするトリガの条件情報
	@param	pri		： 設定するするトリガのプラオリティ（デフォルト3）

	@reruen	recvbuf		： Zabbixから取得した情報
	"""

	getdbinfo(dbg)

	id = env.zbx_id

	# a "as lighter as possible" soap message:

	SM_TEMPLATE_ITEM_SET = """ { "jsonrpc": "2.0", "method": "trigger.create", "params": {
        "priority": "%s", 
	"description": "%s", 
	"hosts": [ { "hostid": "%s" } ], 
	"expression": "%s"
	}, "auth": "%s", "id": %s } """

	auth_data = getzbx_login(id)

	SoapMessage = SM_TEMPLATE_ITEM_SET % (pri,desp,hostid,exp,auth_data,id)
	if dbg in ["1","2"]:
		print SoapMessage

	res = getzbx( SoapMessage )
	recvbuf = json.loads(res)

	if recvbuf.has_key('error'):
		print 'Error occurred.'
		print recvbuf
		return

	if dbg == '1':
		print json.dumps(recvbuf, indent=4)			# print All elements of JSON

	return recvbuf


#============================================================

def zbx_deltrigger(tid,dbg=0):
	"""
	zbx_deltrigger
	Zabbixに指定したホストに指定したtriggerを削除する
	@param	tid		： 削除するするトリガID

	@reruen	recvbuf		： Zabbixから取得した情報
	"""

	getdbinfo(dbg)

	id = env.zbx_id

	# a "as lighter as possible" soap message:

	SM_TEMPLATE_ITEM_SET = """ { "jsonrpc": "2.0", "method": "trigger.delete", "params": [
	"%s"
	], "auth": "%s", "id": %s } """

	auth_data = getzbx_login(id)

	SoapMessage = SM_TEMPLATE_ITEM_SET % (tid,auth_data,id)
	if dbg in ["1","2"]:
		print SoapMessage

	res = getzbx( SoapMessage )
	recvbuf = json.loads(res)

	if recvbuf.has_key('error'):
		print 'Error occurred.'
		print recvbuf
		return

	if dbg == '1':
		print json.dumps(recvbuf, indent=4)			# print All elements of JSON

	return recvbuf


#============================================================

def zbx_get_hostgroup(group_name, dbg=0):
	"""
	zbx_get_hostgroup
	Zabbixから設定されているホストグループを取得する
	@param	None		：

	@reruen	id		： Zabbixから取得したhostgroup
	"""

	getdbinfo(dbg)

	id = env.zbx_id

	SM_TEMPLATE_HOST_GET = """{ "jsonrpc": "2.0", "method": "hostgroup.get", "params": { "output": "extend",
  "filter": { "name": ["%s"] }
	}, "auth": "%s", "id": %s }"""


	auth_data = getzbx_login(id)

	SoapMessage = SM_TEMPLATE_HOST_GET % (group_name, auth_data, id)
	res = getzbx( SoapMessage )
	recvbuf = json.loads(res)

	if recvbuf.has_key('error'):
		print 'Error occurred.'
		print recvbuf
		return

	if dbg == '1':
		print json.dumps(recvbuf, indent=4)			# print All elements of JSON

	return recvbuf

#============================================================
def zbx_gethosts(dbg=0):
	"""
	zbx_gethosts
	Zabbixから設定されているホスト情報を取得する
	@param	None		： 

	@reruen	recvbuf		： Zabbixから取得した情報
	"""

	getdbinfo(dbg)

	id = env.zbx_id

	# a "as lighter as possible" soap message:

	SM_TEMPLATE_HOST_GET = """{ "jsonrpc": "2.0", "method": "host.get", "params": { "output": "extend"
	}, "auth": "%s", "id": %s }"""


	auth_data = getzbx_login(id)

	SoapMessage = SM_TEMPLATE_HOST_GET % (auth_data, id)
	res = getzbx( SoapMessage )
	recvbuf = json.loads(res)

	if recvbuf.has_key('error'):
		print 'Error occurred.'
		print recvbuf
		return

	if dbg == '1':
		print json.dumps(recvbuf, indent=4)			# print All elements of JSON

	return recvbuf

#============================================================

def zbx_gethost(hostname,dbg=0):
	"""
	zbx_gethost
	Zabbixに指定したホストがあるかを確認する
	@param	hostname	： 確認するhost名

	@reruen	recvbuf		： Zabbixから取得した情報
	"""

	getdbinfo(dbg)

	id = env.zbx_id

	# a "as lighter as possible" soap message:

	SM_TEMPLATE_HOST_GET = """{ "jsonrpc": "2.0", "method": "host.get", "params": { "output": "extend",
	"filter": { "host": "%s" }
	}, "auth": "%s", "id": %s }"""

	auth_data = getzbx_login(id)

	SoapMessage = SM_TEMPLATE_HOST_GET % (hostname, auth_data, id)
	res = getzbx( SoapMessage )
	recvbuf = json.loads(res)

	if recvbuf.has_key('error'):
		print 'Error occurred.'
		print recvbuf
		return

	if dbg == '1':
		print json.dumps(recvbuf, indent=4)			# print All elements of JSON

	return recvbuf

#============================================================

def zbx_get_trigger_id(hostid, trigger_name, dbg=0):
	"""
	zbx_get_trigger_id
	ZabbixのTriggerでtrigger_nameに一致するTriggerを取得
	@param	hostid		： 確認するhost名
	@param	trigger_name		： 確認するトリガ情報

	@reruen	recvbuf		： Zabbixから取得した情報
	"""

	getdbinfo(dbg)

	recvbuf = zbx_gettrigger(hostid,1)
	recvkeys = recvbuf.keys()

	if dbg == '1':
		print " Search trigger_name : ",trigger_name

	ret = ''

	for k in recvkeys:
		if k == 'result':
			i = 0
			maxpoint = recvbuf[k]
			max = len(maxpoint)
			while i < max:
				results = recvbuf[k][i]
				reskeys = results.keys()
				if dbg == '1':
					print "  description : ",results['description']
				if trigger_name == results['description']:
					ret = results['triggerid']
					if dbg == '1':
						print "     Matched"

				i = i + 1

	print ret

	return ret

#============================================================

def gettrigger_enable(tid,dbg=0):
	"""
	gettrigger_enable
	Triggerを有効にする
	@param	tid		： 有効にするトリガID

	@reruen	recvbuf		： Zabbixから取得した情報
	"""

	getdbinfo(dbg)

	id = env.zbx_id

	# a "as lighter as possible" soap message:

	SM_TEMPLATE_ITEM_GET = """ { "jsonrpc": "2.0", "method": "trigger.update", "params": {
	"triggerid": "%s",
	"status": 0
	}, "auth": "%s", "id": %s } """

	auth_data = getzbx_login(id)

	SoapMessage = SM_TEMPLATE_ITEM_GET % (tid, auth_data, id)
	res = getzbx( SoapMessage )
	recvbuf = json.loads(res)

	if recvbuf.has_key('error'):
		print 'Error occurred.'
		print recvbuf
		return

	if dbg == '1':
		print json.dumps(recvbuf, indent=4)			# print All elements of JSON

	return recvbuf


#============================================================

def zbx_trigger_disable(tid, dbg=0):
	"""
	gettrigger_enable
	Triggerを無効にする
	@param	tid		： 無効にするトリガID

	@reruen	recvbuf		： Zabbixから取得した情報
	"""

	getdbinfo(dbg)

	id = env.zbx_id

	# a "as lighter as possible" soap message:

	SM_TEMPLATE_ITEM_GET = """ { "jsonrpc": "2.0", "method": "trigger.update", "params": {
	"triggerid": "%s",
	"status": 1
	}, "auth": "%s", "id": %s } """

	auth_data = getzbx_login(id)

	SoapMessage = SM_TEMPLATE_ITEM_GET % (tid, auth_data, id)
	res = getzbx( SoapMessage )
	recvbuf = json.loads(res)

	if recvbuf.has_key('error'):
		print 'Error occurred.'
		print recvbuf
		return

	if dbg == '1':
		print json.dumps(recvbuf, indent=4)			# print All elements of JSON

	return recvbuf


#============================================================

def jos_runjob(jobname,dbg=0):
	"""
	jos_runjob
	JobSchedulerにJob実行のxmlコマンドを送信する
	@param	jobname		： 実行するジョブ名

	@reruen	recvbuf		： Zabbixから取得した情報
	"""

	getdbinfo(dbg)

	# a "as lighter as possible" soap message:

	SM_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
	<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
	<soapenv:Body>
	<startJob xmlns="http://www.sos-berlin.com/scheduler">
	<job>%s</job>
	<at>now</at>
	</startJob>
	</soapenv:Body>
	</soapenv:Envelope>
	"""

	SoapMessage = SM_TEMPLATE % (jobname)

	res = jos_soap(SoapMessage)

	print res

#============================================================

def jos_show_history(jobname,tid,dbg=0):
	"""
	jos_show_history
	JobSchedulerから指定したJobの履歴を取得する
	@param	jobname		： 取得する履歴のするジョブ名
	@param	tid		： 取得する履歴のジョブに対するタスクID

	@reruen	recvbuf		： Zabbixから取得した情報
	"""

	getdbinfo(dbg)

	recvbuf = jos_xml(('<show_history job="%s" id="%d" next="100" />' % (jobname,tid)))
	root = ET.fromstring(recvbuf)

	if dbg == '1':
		xmldoc = minidom.parseString(recvbuf)
		printAllElement(xmldoc.documentElement)

	return root

#============================================================

def jos_show_state(dbg=0):
	"""
	jos_show_state
	JobSchedulerからステータス情報を取得する
	@param	None		： 

	@reruen	recvbuf		： Zabbixから取得した情報
	"""

	getdbinfo(dbg)

	recvbuf = jos_xml('<show_state />')
	if dbg == '1':
		print "===< jos_show_state >==="
		print recvbuf
		print "===< jos_show_state >==="
	root = ET.fromstring(recvbuf)

	if dbg == '1':
		xmldoc = minidom.parseString(recvbuf)
		printAllElement(xmldoc.documentElement)

	return root


#============================================================

def jos_set_server(dbg=0):
	"""
	jos_set_server
	JobSchedulerに使用されてるprocess_classの情報を取得する
	@param	None		： 

	@reruen	None		： （env.jos_server_listに情報を設定する）
	"""

	getdbinfo(dbg)

	check_jobfile()

	path = "%s/%s" % (os.getenv("JM_HOME"), "live")
	if dbg in ["1","2"]:
		print "PATH = ",path

	for job in env.job_list:
		file = "%s/%s" % (path, job)
		if '.process_class.xml' in file:		# process_classファイルだけ処理をする。
			if dbg in ["1","2"]:
				print file

			xdoc = minidom.parse(file)

			elem = xdoc.getElementsByTagName('process_class')
			for s in elem :
				if s.hasAttribute("remote_scheduler"):
					proc_serv = os.path.basename(job).replace('.process_class.xml','')
					env.jos_server_list[proc_serv] = s.attributes['remote_scheduler'].value

	if dbg in ["1","2"]:
		print json.dumps(env.jos_server_list,indent=4)



#============================================================

def set_job_info(dbg=0):
	"""
	set_job_info
	JobSchedulerに登録するJob情報を解析してZabbixに登録するitem情報を取得する
	@param	None		： 

	@reruen	None		： （env.process_classに情報を設定する）
	"""

	if env.jos_job:
		return True

	getdbinfo(dbg)

	source_path = "%s/%s" % (os.getenv("SCHEDULER_DATA"), "config/live")	# 登録情報のディレクトリを取得
	if dbg in ["1","2"]:
		print "set_job_info source path = ",source_path

	for job in env.job_list:
		if '.job.xml' in job:				# JOBファイルだけ処理をする。
			if env.job_list[job] != "DEL":
				file = "%s%s" % (source_path, job)
				job = job.replace('.job.xml','')
				env.jos_job.append(job)

				xdoc = minidom.parse(file)

				elem = xdoc.getElementsByTagName('job')
				for s in elem :
					env.jos_order[job] = 'no'
					if s.hasAttribute("order"):
						env.jos_order[job] = s.attributes['order'].value

					env.process_class[job] = env.jos_server
					if s.hasAttribute("process_class"):
						env.process_class[job] = env.jos_server_list[s.attributes['process_class'].value]
						tmp = env.process_class[job].split(':')
						env.process_class[job] = tmp[0]

	if dbg in ["1","2"]:
		for job in env.jos_job:
			print job
			print '   [set_job_info]process_class = %s' % env.process_class[job]
			print '   [set_job_info]order = %s' % env.jos_order[job]

#============================================================

def set_job_chain_info(dbg=0):
	"""
	set_job_chain_info
	JobSchedulerに登録するJob Chain情報を解析してZabbixに登録するitem情報を取得する
	@param	None		： 

	@reruen	None		： （env.process_classに情報を設定する）
	"""

	if env.jos_job_chain:
		return True

	getdbinfo(dbg)

	source_path = "%s/%s" % (os.getenv("SCHEDULER_DATA"), "config/live")	# 登録情報のディレクトリを取得
	if dbg in ["1","2"]:
		print "[set_job_chain_info]source path = ",source_path

	for job in env.job_list:
		if '.job_chain.xml' in job:				# JOB Chainファイルだけ処理をする。
			if env.job_list[job] != "DEL":
				file = "%s%s" % (source_path, job)
				job = job.replace('.job_chain.xml','')
				env.jos_job_chain.append(job)

				xdoc = minidom.parse(file)

				elem = xdoc.getElementsByTagName('job_chain')
				for s in elem :
					env.process_class[job] = env.jos_server
					if s.hasAttribute("process_class"):
						env.process_class[job] = env.jos_server_list[s.attributes['process_class'].value]
						tmp = env.process_class[job].split(':')
						env.process_class[job] = tmp[0]

	if dbg in ["1","2"]:
		for job in env.jos_job_chain:
			print job
			print '   process_class = %s' % env.process_class[job]

#============================================================

def check_jobfile(dbg=0):
	"""
	check_jobfile
	登録するジョブ情報と現状のジョブ情報を比較してジョブの登録、修正、削除を解析する
	@param	None		： （$JM_HOME/live、$SCHEDULER_DATA/config/live配下のファイル）

	@reruen	None		： （env.job_listに情報を設定する）
	"""

	regist_size={}
	regist_time={}
	live_size={}
	live_time={}

	regist_dirs={}
	live_dirs={}

	getdbinfo(dbg)

	source_path = "%s/%s" % (os.getenv("SCHEDULER_DATA"), "config/live")	# 登録情報のディレクトリを取得
	if dbg in ["1","2"]:
		print "Source path = ",source_path

	for root, dirs, files in os.walk(source_path):			# 登録するファイルの情報取得
	    for file in files:
		if fnmatch.fnmatch(file, '*.xml'):
			file = os.path.join(root, file)
			n = file.replace(source_path,'')
		##	n = n.replace('/','.')
			regist_size[n] = os.stat(file).st_size
			regist_time[n] = os.stat(file).st_mtime

	dest_path = "%s/%s" % (os.getenv("JM_HOME"), "live")	# 現状ファイルの情報取得
	if dbg in ["1","2"]:
		print "dest path = ",dest_path

	for root, dirs, files in os.walk(dest_path):			# 現状ファイルの情報取得
	    for dirs in files:
		if fnmatch.fnmatch(file, '*.xml'):
			file = os.path.join(root, dirs)
			n = file.replace(dest_path,'')
		##	n = n.replace('/','.')
			live_size[n] = os.stat(file).st_size
			live_time[n] = os.stat(file).st_mtime

	for rf in regist_size:					# ファイル情報を比較
		mode = "NONE"
		if live_size.has_key(rf):			# サイズ、日付が違う場合は修正
			if live_size[rf] != regist_size[rf]:
				mode = "MOD"
			elif live_time[rf] != regist_time[rf]:
				mode = "MOD"
		else:						# 現状に無い場合は追加
			mode = "ADD"

		env.job_list[rf] = mode

		if dbg in ["2"]:
			print mode," : ",rf
			print "  REG   size=",regist_size[rf]," : time=",time.ctime(regist_time[rf])
			if mode != "ADD":
				print "  LIV   size=",live_size[rf]," : time=",time.ctime(live_time[rf])

	for rf in live_size:					# 登録ディレクトリにだけある場合は削除
		if regist_size.has_key(rf) == 0:
			env.job_list[rf] = "DEL"
			if dbg in ["2"]:
				print "DEL : ",rf
				print "  LIV   size=",live_size[rf]," : time=",time.ctime(live_time[rf])

	if dbg in ["1","2"]:
		print json.dumps(env.job_list,indent=4)

	for root, dirs, files in os.walk(source_path):			# 登録するディレクトリの情報取得
	    n = root.replace(source_path,'')
	    regist_dirs[n] = n

	for root, dirs, files in os.walk(dest_path):			# 現状ディレクトリの情報取得
	    n = root.replace(dest_path,'')
	    live_dirs[n] = n

	for rd in regist_dirs:					# ファイル情報を比較
		mode = "ADD"
		for ld in live_dirs:
			if rd == ld:
				mode = "NONE"

		if mode == "ADD":
			env.job_dirs[rd] = mode

			if dbg in ["1","2"]:
				print "DIR = ",rd," mode = ",mode

	for ld in live_dirs:					# ファイル情報を比較
		mode = "DEL"
		for rd in regist_dirs:
			if rd == ld:
				mode = "NONE"

		if mode == "DEL":
			env.job_dirs[ld] = mode
			if dbg in ["1","2"]:
				print "DIR = ",ld," mode = ",mode

	if dbg in ["1","2"]:
		print json.dumps(env.job_dirs,indent=4)

#============================================================

def set_copy_jobs(dbg=0):
	"""
	set_copy_jobs
	JobSchedulerへの登録情報ファイルと現状ファイルを合わせる
	@param	None		： （$JM_HOME/live、$SCHEDULER_DATA/config/live配下のファイル）

	@param	None		： （$SCHEDULER_DATA/config/live配下のファイル）
	"""

	getdbinfo(dbg)

	source_path = "%s/%s" % (os.getenv("SCHEDULER_DATA"), "config/live")
	if dbg in ["1","2"]:
		print "Source directory PATH     = ",source_path

	dest_path = "%s/%s" % (os.getenv("JM_HOME"), "live")
	if dbg in ["1","2"]:
		print "Destination directory PATH = ",dest_path

	check_jobfile(dbg)

	if dbg in ["1","2"]:
		print "===<<< Copy to Job & Job Chain Files or Remove >>>==="

	for dir in  env.job_dirs:
		if env.job_dirs[dir] == "ADD":
			cmd = "mkdir -p \"%s%s\"" % (dest_path, dir)

			if dbg in ["1","2"]:
				print(cmd)

			with hide('running'):
				local(cmd)

	for file in  env.job_list:
		cmd = ''
		if env.job_list[file] == "DEL":
			cmd = "rm \"%s/%s\"" % (dest_path, file)

		if env.job_list[file] in ["ADD","MOD"]:
			cmd = "cp -rp \"%s/%s\" \"%s/%s\"" % ( source_path, file[1:], dest_path,  file[1:] )

		if cmd != '':
			if dbg in ["1","2"]:
				print(cmd)

			with hide('running'):
				local(cmd)

	for dir in  env.job_dirs:
		if env.job_dirs[dir] == "DEL":
			cmd = "rm -rf %s%s" % (dest_path, dir)

			if dbg in ["1","2"]:
				print(cmd)

			with hide('running'):
				local(cmd)

#============================================================

def jos_set_last_id(last_id,dbg=0):
	"""
	jos_set_last_id
	DBにlast idを登録する
	@param	last_id		： DBに登録するタスクID情報

	@param	None		： （DBのjobid_tbl）
	"""

	connection = psycopg2.connect(
	 database = env.psqldatabase,
	 user = env.psqluser,
	 password = env.psqlpassword,
	 host = env.psqlhost,
	 port = env.psqlport)

	cur = connection.cursor()

	sql = """delete from jobid_tbl;"""
	cur.execute(sql)
	connection.commit()

	for jname in last_id:
		sql = """insert into jobid_tbl (job,lastid) values ('%s','%d');""" % (jname,last_id[jname])
		if dbg == "1":
			print sql
		cur.execute(sql)

	connection.commit()

	cur.close()
	connection.close()



#############################################################
# メインモジュール
#############################################################

#============================================================

def show_info(dbg=0):
	"""
	show_info
	JobSchedulerからジョブ情報を取得してzabbix_senderでZabbixにジョブの処理時間を送信する
	@param	None		： 

	@param	None		： 
	"""

	last_id={}

	getdbinfo(dbg)

	if dbg in ["1"]:
		print "===< show_info env.last_id Start >==="
		for jid in env.jos_last_id:
			print '  ',jid,' : ',env.jos_last_id[jid]
		print "===< show_info env.last_id End >==="

	for jname in env.jos_last_id:
		last_id[jname] = int(env.jos_last_id[jname],10)

	if dbg in ["1"]:
		print "===< show_info last_id Start >==="
		for jid in last_id:
			print '  ',jid,' : ',last_id[jid]
		print "===< show_info last_id End >==="

	jos_set_server(dbg)
	gethosts(dbg)
	check_jobfile()

	set_job_info(dbg)

	if dbg in ["1"]:
		for serv in env.process_class:
			print "  %s : %s" % (serv,env.process_class[serv])

	root = jos_show_state(dbg)

	elapses = []
	org_time = dt.strptime('1970-01-01 07:00:00','%Y-%m-%d %H:%M:%S')
	for e in root.findall('answer/state/jobs/job/'):	# ジョブの情報を取得
		for name,job in e.items():
			if name == 'path':
				if dbg in ["1"]:
					print 'job name : ',job

				flg = 0
				for jname in last_id:
					if jname == job:
						flg = 1
						break
				if flg == 0:
					last_id[job] = 1

				if dbg == '1':
					print "jos_show_history : %s : %d" % (job,last_id[job])

				root = jos_show_history(job,last_id[job], dbg)
				for elem in root.findall('answer/history/history.entry/'):
					start_time_ut = -1
					end_time_ut = -1
					task = ''
					exit_code = ''
					for n,t in elem.items():
						if n == 'start_time':
							start_time = dt.strptime(t, '%Y-%m-%dT%H:%M:%S.000Z')
							start_time_ut = int(time.mktime(time.strptime(t, '%Y-%m-%dT%H:%M:%S.000Z')))
						elif n == 'end_time':
							end_time = dt.strptime(t, '%Y-%m-%dT%H:%M:%S.000Z')
							end_time_ut = int(time.mktime(time.strptime(t, '%Y-%m-%dT%H:%M:%S.000Z')))
						elif n == 'task':
							task = t
						elif n == 'exit_code':
							exit_code = t

					# Re loop if job is executing
					if start_time_ut == -1 or end_time_ut == -1:
						continue

					elapse = end_time_ut - start_time_ut
					if elapse < 0:
						print '[error] Job elapse is minus error occurred in job[',job,'],start_time[',start_time,'], end_time[',end_time,'], elapse[',elapse,']'
						continue

					item = job.replace('/','.')
					item = item[1:]

					elapses.append("%s job[%s] %s %s" % ( env.process_class[job], item, end_time_ut, elapse))

					if dbg in ["1"]:
						print '    ',task,' : ',exit_code,' :',start_time,' : ',end_time,' -> ',elapse
						print '                  end_time_ut = %s' % end_time_ut
						print cmd

					jid_flg = 0
					for jid in last_id:
						if jid == job:
							jid_flg = 1
							if last_id[jid] < int(task, 10):
								last_id[jid] = int(task, 10)

							break

					if jid_flg == 0:
						last_id[job] = int(task, 10)

	cmd ="echo -e '%s' | /usr/bin/zabbix_sender -z %s -T -i -" % ( "\n".join(elapses), env.zbx_server)
	local( cmd )
	jos_set_last_id(last_id,dbg)

	if dbg in ["1"]:
		print "===< show_info last_id Start >==="
		for jid in last_id:
			print '  ',jid,' : ',last_id[jid]
		print "===< show_info last_id End >==="

#============================================================

def set_job_items(dbg=0):
	"""
	set_job_items
	Zabbixにジョブのitemを設定する
	"""

	getdbinfo(dbg)

	check_jobfile()
	set_copy_jobs(dbg)				# 登録情報ファイルに現状ファイルを合わせる

	jos_set_server(dbg)
	set_job_info(dbg=0)
	set_job_chain_info(dbg=0)

	# setup zabbix host from jobscheduler's process class
	uniq_hosts = set( val for val in env.process_class.values() )
	for hostname in uniq_hosts:
		setup_zbx_host(hostname)

	gethosts(dbg)

	print "===<<< Set Items for Job >>>==="		# Jobのitemの処置
	for job in env.jos_job:
		name = job.replace('/','.')
		name = name.replace('.job.xml','')
		name = name[1:]
		hostid = env.zbx_server_list[env.process_class[job]]

		print '  %s --> %s(%s)' % (name,env.process_class[job],hostid)


	print "===<<< Set Items for Job Chain >>>==="	# Job Chainのitemの処置
	for job in env.jos_job_chain:
		name = job.replace('/','.')
		name = name.replace('.job_chain.xml','')
		name = name[1:]
		hostid = env.zbx_server_list[env.process_class[job]]

		print '  %s --> %s(%s)' % (name,env.process_class[job],hostid)

	set_jobs(dbg)

#============================================================

def set_jobs(dbg=0):
	"""
	set_jobs
	Low Level DiscaveryのJSONデータをzabbix_senderにてZabbix Serverに送信する
	"""

	getdbinfo(dbg)

	jos_set_server(dbg)
	gethosts(dbg)
	check_jobfile()

	set_job_info(dbg=0)

	msg ={}
	for job in env.jos_job:
		name = job.replace('/','.')
		name = name.replace('.job.xml','')
		name = name[1:]
		hostid = env.zbx_server_list[env.process_class[job]]
		itemname = "Jobscheduler\'s Job (%s)" % (name)
		item = {"{#ITEM_NAME}":itemname,"{#JOB_NAME}":name}
		if env.process_class[job] not in msg:
			msg[env.process_class[job]] = []

		msg[env.process_class[job]].append(item)

	for k,v in msg.items():
		err_count = 0
		while True:
			try:
				cmd ="/usr/bin/zabbix_sender -z %s -s %s -k job.discovery -o \"{\\\"data\\\":%s}\"" % ( env.zbx_server, k, json.dumps(v).replace("\"","\\\""))
				local(cmd, capture=True, shell=None)
				break
			except:
				err_count += 1
				if err_count > 2:
					print "[error] Failed to send discovery rule json. Exceed retry timeout in host[%s]" % k
					break
				else:
					print "[warning] Failed to send discovery rule json to host %s. Retry after 60 sec. Retry count: %s/3" % (k, err_count)
					time.sleep(60)

#============================================================

def setup_zbx_host(hostname, dbg = 0):
	"""
	setup_zbx_host
	引数に与えられた情報を元にZabbixのホストをセットアップする
	@param	hostname	: process_classに存在するホスト情報
	"""

	add_zbx_host(hostname, dbg)
	import_zbx_template(dbg = dbg)
	attach_zbx_template(hostname)

def add_zbx_host(zbx_hostname, dbg = 0):
	"""
	add_zbx_host
	引数に与えられた情報を元にZabbixのホストを作成する
  @param	zbx_hostname	: 追加するホスト名
	"""

	zbx_hostid = gethostid(zbx_hostname, dbg)
	if zbx_hostid is not None:
		return

	zbx_hostgroup = zbx_get_hostgroup("Linux servers")
	hostgroup_id = zbx_hostgroup['result'][0]['groupid']

	SM_TEMPLATE_HOST_GET = """
{ "jsonrpc": "2.0", "method": "host.create","params": {
"host": "%s","interfaces": [{"type": 1, "main": 1, "useip": 1, "ip": "127.0.0.1", "dns": "", "port": "10050"}],
"groups": [{ "groupid": "%s" }]}, "auth": "%s", "id": %s }
"""

	auth_data = getzbx_login(env.zbx_id)

	SoapMessage = SM_TEMPLATE_HOST_GET % (zbx_hostname, hostgroup_id, auth_data, env.zbx_id)
	res = getzbx( SoapMessage )
	recvbuf = json.loads(res)

	if recvbuf.has_key('error'):
		print 'Error occurred.'
		print recvbuf
		return

	return recvbuf

def import_zbx_template(file_name = 'hyclops_jm_template.xml', dbg = 0):
	"""
	add_zbx_host
	引数に与えられた情報を元にZabbixのtemplateをimportする
  @param	template_name	: 追加するホスト名
	"""

	template_path = "%s/%s" % (os.getenv("JM_HOME"), file_name)
	template_file = open(template_path).read().replace('\n', ' ').replace('"', '\\"')

	SM_TEMPLATE_HOST_GET = """
{ "jsonrpc": "2.0", "method": "configuration.import","params": {
"format": "xml","source": "%s", "rules": {
"groups": {"createMissing": true}, "applications": {"createMissing": true, "updateExisting": true},
"items": {"createMissing": true, "updateExisting": true},"discoveryRules": {"createMissing": true, "updateExisting": true},
"templates": {"createMissing": true, "updateExisting": true}, "triggers": {"createMissing": true, "updateExisting": true},
"templateLinkage": {"createMissing": true}
}},"auth": "%s", "id": %s }
"""

	auth_data = getzbx_login(env.zbx_id)

	SoapMessage = SM_TEMPLATE_HOST_GET % (template_file, auth_data, env.zbx_id)
	res = getzbx( SoapMessage )
	recvbuf = json.loads(res)

	if recvbuf.has_key('error'):
		print 'Error occurred.'
		print recvbuf
		return

	return recvbuf


def attach_zbx_template(zbx_hostname, template_name = 'Template App HyClops JM', dbg = 0):
	"""
	attach_zbx_tmplate
	引数に与えられた情報を元にZabbixのtemplateをattachする
  @param	zbx_hostname	: 追加するホスト名
	"""

	SM_TEMPLATE_HOST_GET = """
{ "jsonrpc": "2.0", "method": "host.get","params": {
"output": ["hostid"], "selectParentTemplates": "templateid","filter": { "host": "%s" }
},"auth": "%s", "id": %s }
"""

	auth_data = getzbx_login(env.zbx_id)

	SoapMessage = SM_TEMPLATE_HOST_GET % (zbx_hostname, auth_data, env.zbx_id)
	zbx_host = json.loads(getzbx( SoapMessage ))
	if zbx_host is None:
		return

	zbx_hostid = zbx_host['result'][0]['hostid']
	if zbx_hostid is None:
		return

	zbx_templateid = zbx_template_get(template_name)['result'][0]['templateid']
	if zbx_templateid is None:
		return

	templates = zbx_host['result'][0]['parentTemplates']

	missing_hyclops_tmpl = True
	for val in templates:
		if val['templateid'] == zbx_templateid:
			missing_hyclops_tmpl = False

	if missing_hyclops_tmpl:
		templates.append({'templateid': zbx_templateid})

	SM_TEMPLATE_HOST_GET = """
{ "jsonrpc": "2.0", "method": "host.update","params": {
"hostid": "%s", "templates": %s
},"auth": "%s", "id": %s }
"""

	auth_data = getzbx_login(env.zbx_id)

	SoapMessage = SM_TEMPLATE_HOST_GET % (zbx_hostid, json.dumps(templates), auth_data, env.zbx_id)
	res = getzbx( SoapMessage )
	recvbuf = json.loads(res)

	if recvbuf.has_key('error'):
		print 'Error occurred.'
		print recvbuf
		return

	return recvbuf

def zbx_template_get(name, dbg = 0):
	"""
	zbx_template_get
	引数に与えられた情報を元にZabbixのtemplateを取得する
  @param	name	: 取得するtemplate name
	"""

	SM_TEMPLATE_HOST_GET = """
{ "jsonrpc": "2.0", "method": "template.get","params": {
"filter": {"name":["%s"]}
},"auth": "%s", "id": %s }
"""

	auth_data = getzbx_login(env.zbx_id)

	SoapMessage = SM_TEMPLATE_HOST_GET % (name, auth_data, env.zbx_id)
	res = getzbx( SoapMessage )
	recvbuf = json.loads(res)

	if recvbuf.has_key('error'):
		print 'Error occurred.'
		print recvbuf
		return

	return recvbuf

def trigger_switch(hostid, source_trigger_name, rule, dbg=0):
	"""
	trigger_switch
	現状のTriggerを無効にして代わりを設定する
	@param	hostid		： 無効にするホストID
	@param	source_trigger_name		： 無効にするトリガ名
	@param	rule		： 無効後に登録するトリガ条件

	@return	0 or 1		：0 => success, 1 => error
	"""

	success = 0
	error = 1

	source_triggerid = zbx_get_trigger_id(hostid, source_trigger_name, dbg)

	if not source_triggerid:
		print error
		return error

	new_trigger_name = "Switched by HyClops_JobMonitoring(%s)" % source_trigger_name
	new_ret = zbx_set_trigger(hostid, rule, new_trigger_name, 3, dbg)
	if new_ret is None:
		print error
		return error

	source_ret = zbx_trigger_disable(source_triggerid, dbg)
	if source_ret.has_key(u'error'):
		new_trigger_id = new_ret[u'result'][u'triggerids'][0]
		zbx_deltrigger(new_trigger_id)
		print error
		return error

	print success
	return success

#============================================================

def trigger_ret(source_triggerid,dest_triggerid,dbg=0):
	"""
	trigger_ret
	trigger_switchで設定した内容を元に戻す
	@param  source_triggerid		: 有効にするトリガID
	@param	dest_triggerid		: 削除するトリガID

	@param	None		： 
	"""

	gettrigger_enable(source_triggerid,dbg)

	zbx_deltrigger(dest_triggerid,dbg)

	return

#############################################################
# デバッグ、保守用モジュール
#############################################################

#============================================================

def getitems(hostid="10084",dbg=0):
	"""
	getitems
	item一覧を取得する
	@param	hostid		： 取得するアイテムのホストID

	@param	None		： 
	"""

	getdbinfo(dbg)

	recvbuf = zbx_getitems(hostid)
	recvkeys = recvbuf.keys()

	for k in recvkeys:
		if k == 'result':
			i = 0
			maxpoint = recvbuf[k]
			max = len(maxpoint)
			while i < max:
				results = recvbuf[k][i]
				reskeys = results.keys()
				print i," : ",results['itemid']," : ",results['name']," : ",results['key_']," : ",results['description']

				i = i + 1

#============================================================

def gettriggers(hostid="10084",dbg=0):
	"""
	getitems
	Trigger一覧を取得する
	@param	hostid		： 取得するトリガのホストID

	@param	None		： 
	"""

	getdbinfo(dbg)

	recvbuf = zbx_gettrigger(hostid,0)
	recvkeys = recvbuf.keys()

	for k in recvkeys:
		if k == 'result':
			i = 0
			maxpoint = recvbuf[k]
			max = len(maxpoint)
			while i < max:
				results = recvbuf[k][i]
				reskeys = results.keys()
				print i," : ",results['state']," : ",results['triggerid']," : ",results['expression']," : ",results['description']

				i = i + 1

#============================================================

def gethosts(dbg=0):
	"""
	getitems
	ホスト情報を取得し表示する
	@param	None		： 

	@param	None		： 
	"""

	getdbinfo(dbg)

	recvbuf = zbx_gethosts()

	recvkeys = recvbuf.keys()
	for k in recvkeys:
		if k == 'result':
			i = 0
			maxpoint = recvbuf[k]
			max = len(maxpoint)
			while i < max:
				results = recvbuf[k][i]
				reskeys = results.keys()
				env.zbx_server_list[results['host']] = results['hostid']
				if dbg in ["1"]:
					print i," : ",results['hostid']," : ",results['name']," : ",results['host']

				i = i + 1

#============================================================

def gethostid(hostname,dbg=0):
	"""
	getitemid
	ホスト情報を取得し表示する
	@param	hotname		： 取得するホスト名

	@param	None		： 
	"""

	getdbinfo(dbg)

	recvbuf = zbx_gethosts()

	recvkeys = recvbuf.keys()
	for k in recvkeys:
		if k == 'result':
			i = 0
			maxpoint = recvbuf[k]
			max = len(maxpoint)
			while i < max:
				results = recvbuf[k][i]
				reskeys = results.keys()
				env.zbx_server_list[results['host']] = results['hostid']
				if results['name'] == hostname:
					print results['hostid']
					return results['hostid']

				i = i + 1
