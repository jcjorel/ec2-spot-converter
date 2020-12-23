#!/usr/bin/python3
"""
This tool converts EC2 instances back and forth from on-demand and Spot billing models while preserving
network attributes (Private IP addresses, Elastic IP), Storage (Volumes).
It also allows replacement of Spot instances with new ones to update the instance type.

See https://github.com/jcjorel/ec2-spot-converter

Author: jeancharlesjorel@gmail.com
"""
VERSION="::Version::"
RELEASE_DATE="::ReleaseDate::"

import os
import sys
import argparse
import json
import time
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
import pdb

# Configure logging
import logging
from logging import handlers
LOG_LEVEL = logging.INFO
logger = None
def configure_logging():
    global logger
    logger = logging.getLogger(sys.argv[0])
    logger.setLevel(LOG_LEVEL)
    logger.propagate = False
    ch = logging.StreamHandler()
    ch.setLevel(LOG_LEVEL)
    logger_formatter = logging.Formatter("[%(levelname)s] %(asctime)s %(filename)s:%(lineno)d - %(message)s")
    ch.setFormatter(logger_formatter)
    logger.addHandler(ch)

try:
    import boto3
    from botocore.exceptions import ClientError
    from botocore.config import Config
except:
    logger.error("Missing critical dependency: 'boto3'. Please install it with 'python3 -m pip install boto3'.")
    sys.exit(1)

config = Config(
retries = {
    'max_attempts': 5,
    'mode': 'standard'
})
ec2_client               = boto3.client("ec2",               config=config)
dynamodb_client          = boto3.client("dynamodb",          config=config)
elastic_inference_client = boto3.client("elastic-inference", config=config)

def pprint(json_obj):
    return json.dumps(json_obj, indent=4, sort_keys=True, default=str)

def create_state_table(table_name):
    logger.info(f"Creating DynamoDB table '{table_name}'...")
    response = dynamodb_client.create_table(
        TableName=table_name,
        BillingMode='PAY_PER_REQUEST',
        KeySchema=[
            {
                'AttributeName': 'JobId',
                'KeyType': 'HASH'  # Partition key
            },
        ],
        AttributeDefinitions=[
            {
                'AttributeName': 'JobId',
                'AttributeType': 'S'
            },
            {
                'AttributeName': 'State',
                'AttributeType': 'S'
            },
        ],
        GlobalSecondaryIndexes=[
                {
                    'IndexName': 'State',
                    'KeySchema': [
                        {
                            'AttributeName': 'State',
                            'KeyType': 'HASH'
                        },
                    ],
                    'Projection': {
                        'ProjectionType': 'ALL',
                    }
                },
            ],
    )
    logger.debug(response)

def set_state(attribute, value, force_persist=False):
    global states
    table_name = args["dynamodb_tablename"]
    jobid      = args["instance_id"]
    if attribute is None or str(attribute) == "":
        dynamodb_client.delete_item(
            Key = {"JobId": {"S": jobid}},
            TableName=table_name
            )
        states = {}
    else:
        v = json.dumps(value, default=str)
        if not force_persist and attribute in states and states[attribute] == value:
            return # No need to write again the same value
        expression        = f"set {attribute}=:value"
        expression_values = {
            ':value': { 'S': v}
            }
        response = dynamodb_client.update_item(
            Key = {
                'JobId': {
                    'S': jobid
                }
            },
            TableName=table_name,
            ReturnConsumedCapacity='TOTAL',
            ExpressionAttributeValues=expression_values,
            UpdateExpression=expression
            )
        states[attribute] = value
        logger.debug(response)


states = {}
def read_state_table():
    table_name = args["dynamodb_tablename"]
    jobid      = args["instance_id"]
    query      = {
             'JobId': {
                 'S': jobid
             }
    }
    response = dynamodb_client.get_item(
        TableName=table_name,
        ConsistentRead=True,
        ReturnConsumedCapacity='TOTAL',
        Key=query
        )
    logger.debug(response)
    states = defaultdict(str)
    if "Item" not in response:
        return (True, f"Record '{jobid}' doesn't exist yet.", {
            "JobId": jobid
        })
    kv = {}
    for k in response["Item"]:
        if k == "JobId": continue
        kv[k] = json.loads(response["Item"][k]['S'])
    return (True, f"Record '{jobid}' read succesfully.", kv)

def get_instance_details(instance_id=None):
    try:
        instance_id = args["instance_id"] if instance_id is None else instance_id
        Filters   = [{'Name': 'instance-id', 'Values': [instance_id]}]
        response  = ec2_client.describe_instances(Filters=Filters)
        instances = []
        for reservation in response["Reservations"]:
            instances.extend(reservation["Instances"])
        if len(instances) != 1:
            return None
        return instances[0]
    except Exception as e:
        logger.exception(f"Failed to describe instance! {e}")
        return None

def spot_request_need_cancel(spot_request_id, expected_state=[]):
    request = None
    try:
        response      = ec2_client.describe_spot_instance_requests(SpotInstanceRequestIds=[spot_request_id])
        request       = response["SpotInstanceRequests"][0]
        request_state = request["State"]
        if request_state in ["cancelled"]:
            return (False, request)
        elif request_state not in expected_state:
            raise Exception(f"Spot request {spot_request_id} is not in the expected state "
                "(state is '{request_state}', should be among {expected_state})!")
    except ClientError as e:
        if e.response['Error']['Code'] == 'InvalidSpotInstanceRequestID.NotFound':
            return (False, request)
        else:
            raise e
    return (True, request)

def discover_instance_state():
    start_time  = int(time.time())
    instance_id = args["instance_id"]
    instance    = get_instance_details()
    if instance is None:
        raise Exception(f"Can't describe instance '{instance_id}'!")
    logger.debug(pprint(instance))

    # Sanity check
    billing_model              = args["target_billing_model"]
    instance_is_spot           = "SpotInstanceRequestId" in instance
    spot_request               = {}
    spot_request_state         = None
    problematic_spot_condition = False
    if instance_is_spot:
        pdb.set_trace()
        spot_request_id = instance["SpotInstanceRequestId"]
        # Will throw an Exception if something is wrong with the Spot request
        need_cancel, spot_request = spot_request_need_cancel(spot_request_id, ["open", "active", "disabled"])
        if spot_request["Type"] != "persistent":
            return (False, f"Spot instance type is different from 'persistent' one! (current=%s)" % spot_request["Type"])
        # If we can't retrieve the Spot Request or it is already cancelled, we are in bad shape that require special handling.
        problematic_spot_condition = spot_request is None or spot_request["State"] == "cancelled"

    if billing_model == "spot":
        if instance_is_spot:
            cpu_options = None
            if "cpu_options" in args:
                cpu_options = json.loads(args["cpu_options"])
            if (not problematic_spot_condition and
                ("target_instance_type" not in args or instance["InstanceType"] == args["target_instance_type"]) and 
                (cpu_options is None or cpu_options == instance["CpuOptions"])):
                return (False, f"Current instance {instance_id} is already a Spot instance. "
                    "Use --target-billing-model 'on-demand' if you want to convert to 'on-demand' billing model.", {}) 

    if billing_model == "on-demand":
        if not instance_is_spot:
            return (False, f"Current instance {instance_id} is already an On-Demand instance. "
                "Use --target-billing-model 'spot' if you want to convert to 'spot' billing model.", {}) 

    # Warn the user of a problematic condition.
    if problematic_spot_condition:
        logger.warning(f"/!\ WARNING /!\ Spot Instance {instance_id} is linked to an invalid Spot Request '{spot_request_id}'! "
                "This situation is known to create issues like difficulty to stop the instance. "
                "If you encouter an IncorrectSpotRequestState Exception while attempting to stop the instance, please "
                "consider converting the running instance AS-IS with '--do-not-require-stopped-instance' option. "
                "[In order to avoid data consistency issues on the host filesystems either set all filesystems read-only "
                "directly in the host + unmount all possible volumes, or 'SW halt' the system (See your Operating System manual for details)]")
        logger.warning("Pausing 10s... PLEASE READ ABOVE IMPORTANT WARNING!!! DO 'Ctrl-C' NOW IF YOU NEED SOME TIME TO READ!!")
        time.sleep(10)
    if args["do_not_require_stopped_instance"]:
        if args["stop_instance"]:
            logger.warning("/!\ WARNING /!\ --do-not-require-stopped-instance option is set! As --stop-instance is set, a stop command "
                "is going to be tried. If it fails, the conversion will continue anyway.") 
        else:
            logger.warning("/!\ WARNING /!\ --do-not-require-stopped-instance option is set! As --stop-instance is NOT set, "
                "the conversion will start directly on the running instance.") 
        logger.warning("Pausing 10s... PLEASE READ ABOVE IMPORTANT WARNING!!! DO 'Ctrl-C' NOW IF YOU NEED SOME TIME TO READ!!")
        time.sleep(10)

    # 'stopped' state management.
    instance_state = instance["State"]["Name"]
    if instance_state == "stopped":
        return (True, "Instance already in 'stopped' state. Pre-requisite passed.", {
            "InitialInstanceState": instance,
            "SpotRequest": spot_request,
            "FailedStop": False,
            "StartTime": start_time,
            "StartDate": str(datetime.now(tz=timezone.utc))
            })

    if "stop_instance" not in args:
        return (False, f"Instance '{instance_id}' must be in 'stopped' state (current={instance_state}) ! Use --stop-instance if you want to stop it.", {})

    failed_stop = False
    msg         = f"{instance_state} is in state {instance_state}..."
    if instance_state in ["pending", "running"]:
        try:
            msg = f"Stopping '{instance_id}'..."
            ec2_client.stop_instances(InstanceIds=[instance_id])
        except ClientError as e:
            error_code = e.response['Error']['Code']
            if args["do_not_require_stopped_instance"] and error_code == 'IncorrectSpotRequestState':
                msg = str(f"Received an Exception {error_code} while attempting to stop instance. Continue anyway with the running instance as "
                            "--do-not-require-stopped-state option is set.")
                failed_stop = True
            else:
                raise e
    return (True, msg, {
        "InitialInstanceState": instance,
        "SpotRequest": spot_request,
        "FailedStop": failed_stop,
        "StartTime": start_time,
        "StartDate": str(datetime.now(tz=timezone.utc))
        })

def wait_stop_instance():
    failed_stop    = states["FailedStop"]
    instance       = states["InitialInstanceState"]
    instance_id    = instance["InstanceId"]
    instance_state = instance["State"]["Name"]
    max_attempts   = 100
    while not failed_stop and instance_state != "stopped":
        instance = get_instance_details()
        if instance is None:
            return (False, "Can't get instance details! (???)", {})
        instance_state = instance["State"]["Name"]
        if instance_state == "stopped":
            break
        logger.info(f"Waiting for instance to stop... (current state={instance_state})")
        time.sleep(15)
        max_attempts  -= 1
        if max_attempts < 0:
            return (False, "Timeout while waiting for 'stopped' state!", {})

    return (True, f"Instance in '{instance_state}' state.", {
        "ConversionStartInstanceState": instance
        })

def tag_all_resources():
    instance    = states["ConversionStartInstanceState"]
    instance_id = instance["InstanceId"]

    volume_ids  = [blk["Ebs"]["VolumeId"] for blk in instance["BlockDeviceMappings"]]
    eni_ids     = [eni["NetworkInterfaceId"] for eni in instance["NetworkInterfaces"]]

    resources = [instance_id]
    resources.extend(eni_ids)
    resources.extend(volume_ids)
    response  = ec2_client.create_tags(Resources=resources, Tags=[{'Key': 'ec2-spot-converter:job-id', 'Value': instance_id}]) 
    logger.debug(response)
    return (True, f"Successfully tagged {resources}.", {
        "EniIds" : eni_ids
        })

def get_volume_details():
    instance    = states["ConversionStartInstanceState"]
    volume_ids  = [blk["Ebs"]["VolumeId"] for blk in instance["BlockDeviceMappings"]]
    response = ec2_client.describe_volumes(VolumeIds=volume_ids)
    return (True, f"Successfully retrieved volume details for {volume_ids}.", {
        "VolumeDetails": response["Volumes"]
        })


def detach_volumes():
    instance    = states["ConversionStartInstanceState"]
    vol_details = states["VolumeDetails"]
    instance_id = instance["InstanceId"]
    detached_ids= []
    kept_blks   = []
    for blk in instance["BlockDeviceMappings"]:
        vol = blk["Ebs"]["VolumeId"]
        if vol in detached_ids:
            continue
        if not bool(blk["Ebs"]["DeleteOnTermination"]):
            logger.info(f"Detaching volume {vol}...")
            response = ec2_client.detach_volume(Device=blk["DeviceName"], InstanceId=instance_id, VolumeId=vol)
            detached_ids.append(vol)
        else:
            vol_detail = next(filter(lambda v: v["VolumeId"] == vol, vol_details), None)
            b = {
                "DeviceName": blk["DeviceName"],
                "Ebs": {
                    "DeleteOnTermination": blk["Ebs"]["DeleteOnTermination"],
                    "VolumeSize": vol_detail["Size"],
                    "VolumeType": vol_detail["VolumeType"],
                }
            }
            if vol_detail["VolumeType"] not in ["gp2", "st1", "sc1", "standard"]:
                if "Iops"        in vol_detail: b["Ebs"]["Iops"]        = vol_detail["Iops"]
                if "Throughput " in vol_detail: b["Ebs"]["Throughput "] = vol_detail["Throughput"]
            if bool(vol_detail["Encrypted"]):
                b["Ebs"]["Encrypted"] = True
                b["Ebs"]["KmsKeyId"] = vol_detail["KmsKeyId"]
            kept_blks.append(b)
    return (True, f"Detached volumes {detached_ids}.", {
        "DetachedVolumes": detached_ids,
        "VolumesInAMI": kept_blks,
        "WithoutExtraVolumesInstanceState": get_instance_details()
        })

def wait_volume_detach():
    instance    = states["ConversionStartInstanceState"]
    instance_id = instance["InstanceId"]
    volume_ids  = states["DetachedVolumes"]
    max_attempts = 300/5
    while max_attempts and len(volume_ids):
        response = ec2_client.describe_volumes(VolumeIds=volume_ids)
        logger.debug(response)
        max_attempts -= 1
        not_avail = [vol for vol in response["Volumes"] if vol["State"] != "available"]
        for vol in not_avail:
            if vol["MultiAttachEnabled"]:
                stilled_attached = next(filter(lambda a: a["InstanceId"] == instance_id, vol["Attachments"]), None)
                logger.info(f"Detected multi-attached volume '%s'. Taking care of this special case..." % vol["VolumeId"]) 
                if stilled_attached is None:
                    not_avail.remove(vol)
        if len(not_avail) == 0:
            break
        if max_attempts % 5 == 0:
            logger.info("Waiting for detached volumes to become 'available'...")
        time.sleep(5)
    if max_attempts == 0:
        return (False, f"Not all volume where 'available' before timeout : {volume_ids}.", {})
    time.sleep(1) # Superstition...
    return (True, f"All detached volumes are 'available' : {volume_ids}.", {})

def create_ami():
    instance    = states["WithoutExtraVolumesInstanceState"]
    kepts_blks  = states["VolumesInAMI"]
    instance_id = instance["InstanceId"]
    image_name  = f"ec2-spot-converter-{instance_id}"
    try:
        logger.info(f"AMI Block device mapping: {kepts_blks}")
        response= ec2_client.create_image(Name=image_name, InstanceId=instance_id, BlockDeviceMappings=kepts_blks)
        logger.debug(response)
    except ClientError as e:
        if e.response['Error']['Code'] == 'InvalidAMIName.Duplicate':
            return (False, f"Image '{image_name}' already exists! Please delete it and restart the tool...", {})

    response = ec2_client.describe_images(Filters=[{
        'Name': 'name',
        'Values': [image_name]
        }])
    image_id = response["Images"][0]["ImageId"]
    return (True, f"AMI image {image_name}/{image_id} started.", {
        "ImageId": image_id
        })


def prepare_network_interfaces():
    instance    = states["WithoutExtraVolumesInstanceState"]
    instance_id = instance["InstanceId"]

    for eni in instance["NetworkInterfaces"]:
        eni_id   = eni["NetworkInterfaceId"]
        response = ec2_client.modify_network_interface_attribute(
            Attachment={
                'AttachmentId': eni["Attachment"]["AttachmentId"],
                'DeleteOnTermination': False
            },
            NetworkInterfaceId=eni_id)
        logger.debug(response)
    return (True, f"Successfully prepared network interfaces %s." % states["EniIds"], {})

def wait_ami():
    ami_id       = states["ImageId"]
    max_attempts = 120 
    image_state  = ""
    while max_attempts and image_state != "available":
        response      = ec2_client.describe_images(ImageIds=[ami_id])
        image_state   = response["Images"][0]["State"]
        if image_state == "available":
            break
        logger.info(f"Waiting for image {ami_id} to be available...")
        time.sleep(20)
        max_attempts -= 1
    if max_attempts == 0:
        return (False, f"AMI {ami_id} creation timeout!", {})
    return (True, f"AMI {ami_id} is ready..", {})

def instance_state_checkpoint():
    instance     = states["WithoutExtraVolumesInstanceState"]
    elastic_gpus = []
    if "ElasticGpuAssociations" in instance:
        gpu_ids = []
        for gpu in instance["ElasticGpuAssociations"]:
            gpu_id = gpu["ElasticGpuId"]
            gpu_ids.append(gpu_id)
        response = ec2_client.describe_elastic_gpus(ElasticGpuIds=gpu_ids)
        elastic_gpus = response["ElasticGpuSet"]
    return (True, "Checkpointed instance state...", {
        "InstanceStateCheckpoint": get_instance_details(),
        "ElasticGPUs": elastic_gpus
        })

def terminate_instance():
    instance    = states["InstanceStateCheckpoint"]
    instance_id = instance["InstanceId"]

    if "SpotInstanceRequestId" in instance:
        spot_request_id = instance["SpotInstanceRequestId"]
        need_cancel, request = spot_request_need_cancel(spot_request_id, ["disabled"]) # We require the spot request to be in 'disabled' state.
        if need_cancel: 
            logger.info(f"Cancelling Spot request {spot_request_id}...")
            response = ec2_client.cancel_spot_instance_requests(SpotInstanceRequestIds=[spot_request_id])
            logger.debug(response)
            time.sleep(5)

    response = ec2_client.terminate_instances(InstanceIds=[instance_id])
    logger.debug(response)
    return (True, f"Successfully terminated instance {instance_id}.", {})

def wait_resource_release():
    instance    = states["InstanceStateCheckpoint"]
    eni_ids       = [eni["NetworkInterfaceId"] for eni in instance["NetworkInterfaces"]]
    max_attempts = 300/5
    while max_attempts and len(eni_ids):
        response = ec2_client.describe_network_interfaces(NetworkInterfaceIds=eni_ids)
        logger.debug(response)
        max_attempts -= 1
        not_avail = [vol for vol in response["NetworkInterfaces"] if vol["Status"] != "available"]
        if len(not_avail) == 0:
            break
        if max_attempts % 5 == 0:
            logger.info("Waiting for detached ENIs to become 'available'...")
        time.sleep(5)
    if max_attempts == 0:
        return (False, f"Not all ENIs where 'available' before timeout : {eni_ids}.", {})
    time.sleep(2) # Superstition... Without proofs, we BELIEVE that it is good to not rush create the new instance...
    return (True, f"All resources released : {eni_ids}.", {})

def create_new_instance():
    instance     = states["InstanceStateCheckpoint"]
    kepts_blks   = states["VolumesInAMI"]
    ami_id       = states["ImageId"]
    elastic_gpus = states["ElasticGPUs"]

    ifaces = []
    for eni in instance["NetworkInterfaces"]:
        ifaces.append({
            'DeviceIndex': eni["Attachment"]["DeviceIndex"],
            'NetworkInterfaceId': eni["NetworkInterfaceId"],
        })

    block_devs = []
    for blk in instance["BlockDeviceMappings"]:
        if next(filter(lambda b: b["DeviceName"] == blk["DeviceName"], kepts_blks), None) is None:
            continue 
        b = { "DeviceName": blk["DeviceName"] }
        if bool(instance["EbsOptimized"]):
            ebs      = blk["Ebs"]
            volume   = next(filter(lambda v: v["VolumeId"] == ebs["VolumeId"], states["VolumeDetails"]), None)
            if volume is None:
                raise Exception("Can't find Volume '%s'! Should never happen!! (Bug?)" % ebs["VolumeId"])
            b["Ebs"] = {}
            if "VolumeType" in volume: 
                voltype = volume["VolumeType"]
                b["Ebs"]["VolumeType"] = voltype
                if "Iops" in volume and voltype != "gp2":       b["Ebs"]["Iops"]       = volume["Iops"]
                if "Throughput" in volume and voltype != "gp2": b["Ebs"]["Throughput"] = volume["Throughput"]
            if "Encrypted"  in volume: b["Ebs"]["Encrypted"]  = volume["Encrypted"]
            if "KmsKeyId"   in volume: b["Ebs"]["KmsKeyId"]   = volume["KmsKeyId"]
        block_devs.append(b)
    logger.debug(f"New instance Block Device mappings : {block_devs}")

    launch_specifications = {
            'BlockDeviceMappings': block_devs,
            'EbsOptimized': instance["EbsOptimized"],
            'ImageId': ami_id,
            'InstanceType': instance["InstanceType"] if not "target_instance_type" in args else args["target_instance_type"],
            'KeyName': instance["KeyName"],
            'Monitoring': {
                'Enabled': instance["Monitoring"]["State"] in ["enabled", "pending"],
            },
            'CapacityReservationSpecification': instance["CapacityReservationSpecification"],
            'HibernationOptions': instance["HibernationOptions"],
            'NetworkInterfaces': ifaces,
            'Placement': {
                'AvailabilityZone': instance["Placement"]["AvailabilityZone"],
                'Tenancy': instance["Placement"]["Tenancy"]
            },
            'MaxCount': 1,
            'MinCount': 1
        }
    if "InstanceInitiatedShutdownBehavior" in instance:
        launch_specifications['InstanceInitiatedShutdownBehavior']: instance["InstanceInitiatedShutdownBehavior"]

    if "ElasticGpuAssociations" in instance:
        launch_specifications['ElasticGpuSpecification'] = []
        for gpu in elastic_gpus:
            launch_specifications['ElasticGpuSpecification'].append({
                "Type": gpu["ElasticGpuType"]
                })

    if "ElasticInferenceAcceleratorAssociations" in instance: 
        elastic_inf = [ i["ElasticInferenceAcceleratorArn"] for i in instance["ElasticInferenceAcceleratorAssociations"]][0].split("/")[1]
        response = elastic_inference_client.describe_accelerators(acceleratorIds=[elastic_inf])
        if len(response["acceleratorSet"]) != 1:
            return (False, f"Can't describe Elastic Inference id '{elastic_inf}'... (IAM permissions missing?)", {})
        launch_specifications["ElasticInferenceAccelerators"] = {
                "Type": response["acceleratorSet"]["acceleratorType"],
                "Count": len(instance["ElasticInferenceAcceleratorAssociations"])
            }

    if "IamInstanceProfile" in instance:
        launch_specifications["IamInstanceProfile"] = {
            "Arn": instance["IamInstanceProfile"]["Arn"]
            }
    if "UserData" in instance:
        launch_specifications["UserData"] = instance["UserData"] 
    #if "ClientToken" in instance and instance["ClientToken"] != "":
    #    launch_specifications["ClientToken"] = instance["ClientToken"]
    if "CpuOptions" in instance and not "target_instance_type" in args:
        # Preserve CpuOptions only if we do not force the instance type
        launch_specifications["CpuOptions"] = instance["CpuOptions"]
    if "cpu_options" in args:
        launch_specifications["CpuOptions"] = json.loads(args["cpu_options"])
    if "CreditSpecification" in instance:
        launch_specifications["CreditSpecification"] = instance["CreditSpecification"]
    if "Tags" in instance:
        launch_specifications["TagSpecifications"] = [{
            "ResourceType": "instance",
            "Tags": instance["Tags"]
            }]
    if args["target_billing_model"] == "spot":
        launch_specifications["InstanceMarketOptions"] = {
            'MarketType': 'spot',
            'SpotOptions': {
                'SpotInstanceType': 'persistent',
                'InstanceInterruptionBehavior': 'stop'
                }
            }
        if "max_spot_price" in args:
            launch_specifications["InstanceMarketOptions"]["MaxPrice"] = args[max_spot_price]

    response = ec2_client.run_instances(**launch_specifications)
    logger.debug(response)
    new_instance_id = response["Instances"][0]["InstanceId"]
    return (True, f"Created new instance '{new_instance_id}'.", {
        "NewInstanceId": new_instance_id,
        })

def wait_new_instance():
    instance_id  = states["NewInstanceId"]
    status_code  = ""
    max_attempts = 600
    while max_attempts and status_code != "running":
        instance      = get_instance_details(instance_id=instance_id)
        if instance is not None:
            status_code   = instance["State"]["Name"]
        max_attempts -= 1
        if max_attempts % 30 == 0:
            logger.info("Waiting for instance to come up...")
        time.sleep(0.5)
    if max_attempts == 0:
        return (False, "The new instance did not come up before timeout!", {})

    instance_id = instance["InstanceId"]
    return (True, f"Instance {instance_id} is in 'running' state.", {
        "NewInstanceDetails": instance
        })

def reattach_volumes():
    instance_id   = states["NewInstanceId"]
    orig_instance = states["ConversionStartInstanceState"]
    volume_ids    = states["DetachedVolumes"]
    
    current_blks  = get_instance_details(instance_id=instance_id)["BlockDeviceMappings"]
    attached_ids  = []
    for vol in volume_ids:
        if next(filter(lambda v: v["Ebs"]["VolumeId"] == vol, current_blks), None) is not None:
            continue # Already attached. May happen when forcibly replying the step.
        if vol in attached_ids:
            continue
        blk         = next(filter(lambda v: v["Ebs"]["VolumeId"] == vol, orig_instance["BlockDeviceMappings"]), None)
        device_name = blk["DeviceName"]
        logger.info(f"Attaching volume {vol} to {instance_id} with device name {device_name}...")
        response = ec2_client.attach_volume(
            Device=device_name,
            InstanceId=instance_id,
            VolumeId=vol)
        attached_ids.append(vol)
    if len(attached_ids) and not args["reboot_if_needed"]:
        logger.warning("/!\ WARNING /!\ Volumes attached after boot. YOUR NEW INSTANCE MAY NEED A REBOOT!")
    return (True, f"Successfully reattached volumes {attached_ids}...", {
        "ReattachedVolumesInstanceState": get_instance_details(instance_id=instance_id)
        })

def configure_network_interfaces():
    new_instance  = states["ReattachedVolumesInstanceState"]
    orig_instance = states["ConversionStartInstanceState"]
    for eni in new_instance["NetworkInterfaces"]:
        eni_id   = eni["NetworkInterfaceId"]
        orig_eni = next(filter(lambda e: eni["NetworkInterfaceId"] == eni_id, orig_instance["NetworkInterfaces"]), None)
        if bool(orig_eni["Attachment"]["DeleteOnTermination"]): 
            logger.info(f"Setting 'DeleteOnTermination=True' for interface {eni_id}...")
            response = ec2_client.modify_network_interface_attribute(
                Attachment={
                    'AttachmentId': eni["Attachment"]["AttachmentId"],
                    'DeleteOnTermination': True
                },
                NetworkInterfaceId=eni_id)
            logger.debug(response)
    return (True, f"Successfully configured network interfaces %s." % states["EniIds"], {})

def manage_elastic_ip():
    instance          = states["InitialInstanceState"]
    instance_id       = states["NewInstanceId"]
    response          = ec2_client.describe_addresses()
    eips              = response["Addresses"]
    reassociated_eips = []
    for eni in instance["NetworkInterfaces"]:
        if "Association" in eni:
            public_ip = eni["Association"]["PublicIp"]
            eip       = next(filter(lambda eip: eip["PublicIp"] == public_ip, eips), None)
            if eip is not None:
                response  = ec2_client.associate_address(
                    AllocationId=eip["AllocationId"],
                    NetworkInterfaceId=eni["NetworkInterfaceId"],
                    )
                reassociated_eips.append(eip["PublicIp"])
                logger.debug(response)
    return (True, f"Reassociated EIPs '{reassociated_eips}'...", {})

def reboot_if_needed():
    instance_id   = states["NewInstanceId"]
    volume_ids    = states["DetachedVolumes"]
    if len(volume_ids) == 0:
        return (True, f"No reason to reboot instance '{instance_id}'... Skipping...", {
            "Rebooted": False
            })

    if not args["reboot_if_needed"]:
        return (True, f"It is recommended to reboot '{instance_id}' but --reboot-if-needed option not in the command line: Do nothing.", {
            "Rebooted": False
            })

    response = ec2_client.reboot_instances(InstanceIds=[instance_id])
    logger.debug(response)
    return (True, f"Successfully rebooted '{instance_id}'.", {
        "Rebooted": True
        })

def untag_resources():
    instance_id   = states["NewInstanceId"]
    orig_instance = states["ConversionStartInstanceState"]
    eni_ids       = [eni["NetworkInterfaceId"] for eni in orig_instance["NetworkInterfaces"]]
    volume_ids    = states["DetachedVolumes"]
    resources     = [instance_id]
    resources.extend(eni_ids)
    resources.extend(volume_ids)

    response  = ec2_client.delete_tags(Resources=resources, Tags=[{'Key': 'ec2-spot-converter:job-id'}]) 
    logger.debug(response)
    return (True, f"Successfully untagged {resources}.", {
        "FinalInstanceState": get_instance_details(instance_id=instance_id),
        "EndTime": time.time()
        })

def deregister_image():
    ami_id = states["ImageId"]

    response = ec2_client.describe_images(ImageIds=[ami_id])
    logger.debug(response)

    img      = response["Images"][0]
    snap_ids = [blk["Ebs"]["SnapshotId"] for blk in img["BlockDeviceMappings"]]
    response = ec2_client.deregister_image(ImageId=ami_id)
    logger.debug(response)

    time.sleep(0.5)
    for snap_id in snap_ids:
        logger.info(f"Deleting snapshot '{snap_id}'...")
        response = ec2_client.delete_snapshot(SnapshotId=snap_id)
        logger.debug(response)

    return (True, f"Successfully deregistered AMI '{ami_id}'.", {
        })

steps = [
    {
        "Name" : "read_state_table",
        "PrettyName" : "ReadConfig",
        "Function": read_state_table,
        "Description": "Read DynamoDB state table..."
    },
    {
        "Name" : "discover_instance_state",
        "PrettyName" : "DiscoverInstanceState",
        "Function": discover_instance_state,
        "Description": "Discover instance state..."
    },
    {
        "Name" : "wait_stop_instance",
        "PrettyName" : "WaitStopInstance",
        "Function": wait_stop_instance,
        "Description": "Wait for 'stopped' instance state..."
    },
    {
        "Name" : "tag_all_resources",
        "PrettyName" : "TagAllResources",
        "Function": tag_all_resources,
        "Description": "Tag all resources (Instance, ENI(s), Volumes) with ec2-spot-converter job Id..."
    },
    {
        "Name" : "get_volume_details",
        "PrettyName" : "GetVolumeDetails",
        "Function": get_volume_details,
        "Description": "Get storage volume details..."
    },
    {
        "Name" : "detach_volumes",
        "PrettyName" : "DetachVolumes",
        "Function": detach_volumes,
        "Description": "Detach instance volumes with DeleteOnTermination=False..."
    },
    {
        "Name" : "wait_volume_detach",
        "PrettyName" : "WaitVolumeDetach",
        "Function": wait_volume_detach,
        "Description": "Wait for volume detach status..."
    },
    {
        "Name" : "create_ami",
        "PrettyName" : "CreateAmi",
        "Function": create_ami,
        "Description": "Start AMI creation..."
    },
    {
        "Name" : "prepare_network_interfaces",
        "PrettyName" : "PrepareNetworkInterfaces",
        "Function": prepare_network_interfaces,
        "Description": "Prepare network interfaces for instance disconnection..."
    },
    {
        "Name" : "wait_ami",
        "PrettyName" : "WaitAmi",
        "Function": wait_ami,
        "Description": "Wait for AMI to be ready..."
    },
    {
        "Name" : "instance_state_checkpoint",
        "PrettyName" : "InstanceStateCheckpoint",
        "Function": instance_state_checkpoint,
        "Description": "Checkpoint the current exact state of the instance..."
    },
    {
        "Name" : "terminate_instance",
        "PrettyName" : "TerminateInstance",
        "Function": terminate_instance,
        "Description": "Terminate instance..."
    },
    {
        "Name" : "wait_resource_release",
        "PrettyName" : "WaitResourceRelease",
        "Function": wait_resource_release,
        "Description": "Wait resource release..."
    },
    {
        "Name" : "create_new_instance",
        "PrettyName" : "CreateInstance",
        "Function": create_new_instance,
        "Description": "Create new instance..."
    },
    {
        "Name" : "wait_new_instance",
        "PrettyName" : "WaitNewInstance",
        "Function": wait_new_instance,
        "Description": "Wait new instance to come up..."
    },
    {
        "Name" : "reattach_volumes",
        "PrettyName" : "ReattachVolumes",
        "Function": reattach_volumes,
        "Description": "Reattach volumes..."
    },
    {
        "Name" : "configure_network_interfaces",
        "PrettyName" : "ConfigureNetworkInterfaces",
        "Function": configure_network_interfaces,
        "Description": "Configure network interfaces..."
    },
    {
        "Name" : "manage_elastic_ip",
        "PrettyName" : "ManageElasticIP",
        "Function": manage_elastic_ip,
        "Description": "Manage Elastic IP..."
    },
    {
        "Name" : "reboot_if_needed",
        "PrettyName" : "RebootIfNeeded",
        "Function": reboot_if_needed,
        "Description": "Reboot new instance (if needed and requested)..."
    },
    {
        "Name" : "untag_resources",
        "PrettyName" : "UntagResources",
        "Function": untag_resources,
        "Description": "Untag resources..."
    },
    {
        "Name" : "deregister_image",
        "IfArgs": "delete_ami",
        "PrettyName" : "DeregisterImage",
        "Function": deregister_image,
        "Description": "Deregister image..."
    }
]

def review_conversion_results():
    o_file = tempfile.NamedTemporaryFile(prefix="original_instance-", suffix=".json", buffering=0)   
    initial_instance_state = states["InitialInstanceState"]
    initial_instance_state["NetworkInterfaces"] = sorted(initial_instance_state["NetworkInterfaces"], 
            key=lambda i: int(i["Attachment"]["DeviceIndex"]))
    initial_instance_state["Tags"] = sorted(initial_instance_state["Tags"], key=lambda t: t["Key"])
    o_file.write(bytes(pprint(initial_instance_state), "utf-8"))
    n_file = tempfile.NamedTemporaryFile(prefix="new_instance-", suffix=".json", buffering=0)   
    final_instance_state   = states["FinalInstanceState"]
    final_instance_state["NetworkInterfaces"] = sorted(final_instance_state["NetworkInterfaces"], 
            key=lambda i: int(i["Attachment"]["DeviceIndex"]))
    final_instance_state["Tags"] = sorted(final_instance_state["Tags"], key=lambda t: t["Key"])
    n_file.write(bytes(pprint(final_instance_state), "utf-8"))
    os.system("vim -c ':syntax off' -d %s %s" % (o_file.name, n_file.name))
    o_file.close()
    n_file.close()


args = {}
default_args = {
        "dynamodb_tablename": "ec2-spot-converter-state-table",
        "target_billing_model": "spot",
        "reboot_if_needed": False,
        "delete_ami": False,
        "do_not_require_stopped_instance": False
    }
if __name__ == '__main__':
    if "--version" in sys.argv or "-v" in sys.argv:
        print(f"{VERSION} ({RELEASE_DATE})")
        sys.exit(0)

    generate_db = "--generate-dynamodb-table" in sys.argv
    parser = argparse.ArgumentParser(description=f"EC2 Spot converter {VERSION} ({RELEASE_DATE})")
    parser.add_argument('-i', '--instance-id', help="The id of the EC2 instance to convert.", 
            type=str, required=not generate_db, default=argparse.SUPPRESS)
    parser.add_argument('-m', '--target-billing-model', help="The expected billing model after conversion. "
            "Default: 'spot'", choices=["spot", "on-demand"],
            type=str, required=False, default=argparse.SUPPRESS)
    parser.add_argument('-t', '--target-instance-type', help="The expected instance type (ex: m5.large...) after conversion. "
            "This flag is only useful when applied to a Spot instance as EC2.modify_instance_attribute() can't be used "
            "to change Instance type. "
            "Default: <original_instance_type>", 
            type=str, required=False, default=argparse.SUPPRESS)
    parser.add_argument('--cpu-options', help="Instance CPU Options JSON structure. "
            'Format: {"CoreCount":123,"ThreadsPerCore":123}.',
            type=str, required=False, default=argparse.SUPPRESS)
    parser.add_argument('--max-spot-price', help="Maximum hourly price for Spot instance target.", 
            type=str, required=False, default=argparse.SUPPRESS)
    parser.add_argument('--dynamodb-tablename', help="A DynamoDB table name to hold conversion states. "
            "Default: 'ec2-spot-converter-state-table'", 
            type=str, required=False, default=argparse.SUPPRESS)
    parser.add_argument('--generate-dynamodb-table', help="Generate a DynamoDB table name to hold conversion states.",
            action='store_true', required=False, default=argparse.SUPPRESS)
    parser.add_argument('-s', '--stop-instance', help="Stop instance instead of failing because it is in 'running' state.",
            action='store_true', required=False, default=argparse.SUPPRESS)
    parser.add_argument('--reboot-if-needed', help="Reboot the new instance if needed.", 
            action='store_true', required=False, default=argparse.SUPPRESS)
    parser.add_argument('--delete-ami', help="Delete AMI at end of conversion.", 
            action='store_true', required=False, default=argparse.SUPPRESS)
    parser.add_argument('-d', '--debug', help="Turn on debug traces.", 
            action='store_true', required=False, default=argparse.SUPPRESS)
    parser.add_argument('-v', '--version', help="Display tool version.", 
            action='store_true', required=False, default=argparse.SUPPRESS)
    parser.add_argument('--do-not-require-stopped-instance', help="Allow instance conversion while instance is in 'running' state. (NOT RECOMMENDED)", 
            action='store_true', required=False, default=argparse.SUPPRESS)
    parser.add_argument('-r', '--review-conversion-result', help="Display side-by-side conversion result. Note: REQUIRES 'VIM' EDITOR!", 
            action='store_true', required=False, default=argparse.SUPPRESS)
    cmdargs = {}
    for a in parser.parse_args()._get_kwargs():
        cmdargs[a[0]] = a[1]
    args = cmdargs.copy()
    for a in default_args:
        if a not in args or args[a] is None: args[a] = default_args[a]

    LOG_LEVEL=logging.DEBUG if "debug" in args else logging.INFO
    configure_logging()

    if "generate_dynamodb_table" in cmdargs:
        create_state_table(args["dynamodb_tablename"])
        sys.exit(0)

    start_time = time.time()
    step_names = [s["Name"] for s in steps]
    for i in range(0, len(steps)):
        step      = steps[i]
        step_name = step["Name"]
        if "IfArgs" in step and not args[step["IfArgs"]]:
            logger.info(f"[STEP %d/%d] %s => SKIPPED! Need '--%s' argument." % 
                    (i + 1, len(steps), step["Description"], step["IfArgs"].replace("_","-")))
            continue
        display_status = ""
        if "ConversionStep" in states:
            current_step=states["ConversionStep"]
            last_succes_step = step_names.index(current_step)
            if i <= last_succes_step:
                display_status = ": RECOVERED STATE. SKIPPED!"
        logger.info(f"[STEP %d/%d] %s %s" % (i + 1, len(steps), step["Description"], display_status))
        if display_status != "":
            if "ConversionStepReasons" in states and step_name in states["ConversionStepReasons"]:
                logger.info(f"  => SUCCESS. %s" % states["ConversionStepReasons"][step_name])
            continue

        return_code, reason, keys = step["Function"]()
        if not return_code:
            logger.error(f"Failed to perform step '%s'! Reason={reason}" % step["PrettyName"])
            sys.exit(1)
        logger.info(f"  => SUCCESS. {reason}")
        set_state("ConversionStep", step_name)
        reasons = states["ConversionStepReasons"] if "ConversionStepReasons" in states else {}
        reasons[step_name] = reason
        set_state("ConversionStepReasons", reasons, force_persist=True)
        for s in keys:
            if s != "JobId": set_state(s, keys[s])

    logger.debug(pprint(states["FinalInstanceState"]))
    logger.info("Conversion successful! New instance id: %s, ElapsedTime: %s seconds" % 
            (states["NewInstanceId"], int(states["EndTime"] - states["StartTime"]))) 

    if args["review_conversion_result"]:
        review_conversion_results()

    if "DetachedVolumes" in states and len(states["DetachedVolumes"]) and not args["reboot_if_needed"]:
        logger.warning("/!\ WARNING /!\ Volumes attached after boot. YOUR NEW INSTANCE MAY NEED A REBOOT!")

